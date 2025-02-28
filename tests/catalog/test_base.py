#  Licensed to the Apache Software Foundation (ASF) under one
#  or more contributor license agreements.  See the NOTICE file
#  distributed with this work for additional information
#  regarding copyright ownership.  The ASF licenses this file
#  to you under the Apache License, Version 2.0 (the
#  "License"); you may not use this file except in compliance
#  with the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing,
#  software distributed under the License is distributed on an
#  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#  KIND, either express or implied.  See the License for the
#  specific language governing permissions and limitations
#  under the License.
# pylint:disable=redefined-outer-name


import uuid
from pathlib import PosixPath
from typing import (
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import pyarrow as pa
import pytest
from pydantic_core import ValidationError
from pytest_lazyfixture import lazy_fixture

from pyiceberg.catalog import Catalog, MetastoreCatalog, PropertiesUpdateSummary, load_catalog
from pyiceberg.exceptions import (
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NoSuchNamespaceError,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.io import WAREHOUSE, load_file_io
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import (
    CommitTableResponse,
    Table,
)
from pyiceberg.table.metadata import new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.table.update import (
    AddSchemaUpdate,
    SetCurrentSchemaUpdate,
    TableRequirement,
    TableUpdate,
    update_table_metadata,
)
from pyiceberg.transforms import IdentityTransform
from pyiceberg.typedef import EMPTY_DICT, Identifier, Properties
from pyiceberg.types import IntegerType, LongType, NestedField

DEFAULT_WAREHOUSE_LOCATION = "file:///tmp/warehouse"


class InMemoryCatalog(MetastoreCatalog):
    """
    An in-memory catalog implementation that uses in-memory data-structures to store the namespaces and tables.

    This is useful for test, demo, and playground but not in production as data is not persisted.
    """

    __tables: Dict[Identifier, Table]
    __namespaces: Dict[Identifier, Properties]

    def __init__(self, name: str, **properties: str) -> None:
        super().__init__(name, **properties)
        self.__tables = {}
        self.__namespaces = {}
        self._warehouse_location = properties.get(WAREHOUSE, DEFAULT_WAREHOUSE_LOCATION)

    def create_table(
        self,
        identifier: Union[str, Identifier],
        schema: Union[Schema, "pa.Schema"],
        location: Optional[str] = None,
        partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC,
        sort_order: SortOrder = UNSORTED_SORT_ORDER,
        properties: Properties = EMPTY_DICT,
        table_uuid: Optional[uuid.UUID] = None,
    ) -> Table:
        schema: Schema = self._convert_schema_if_needed(schema)  # type: ignore

        identifier = Catalog.identifier_to_tuple(identifier)
        namespace = Catalog.namespace_from(identifier)

        if identifier in self.__tables:
            raise TableAlreadyExistsError(f"Table already exists: {identifier}")
        else:
            if namespace not in self.__namespaces:
                self.__namespaces[namespace] = {}

            if not location:
                location = f"{self._warehouse_location}/{'/'.join(identifier)}"
            location = location.rstrip("/")

            metadata_location = self._get_metadata_location(location=location)
            metadata = new_table_metadata(
                schema=schema,
                partition_spec=partition_spec,
                sort_order=sort_order,
                location=location,
                properties=properties,
                table_uuid=table_uuid,
            )
            io = load_file_io({**self.properties, **properties}, location=location)
            self._write_metadata(metadata, io, metadata_location)

            table = Table(
                identifier=identifier,
                metadata=metadata,
                metadata_location=metadata_location,
                io=io,
                catalog=self,
            )
            self.__tables[identifier] = table
            return table

    def register_table(self, identifier: Union[str, Identifier], metadata_location: str) -> Table:
        raise NotImplementedError

    def commit_table(
        self, table: Table, requirements: Tuple[TableRequirement, ...], updates: Tuple[TableUpdate, ...]
    ) -> CommitTableResponse:
        identifier_tuple = table.name()
        current_table = self.load_table(identifier_tuple)
        base_metadata = current_table.metadata

        for requirement in requirements:
            requirement.validate(base_metadata)

        updated_metadata = update_table_metadata(base_metadata, updates)
        if updated_metadata == base_metadata:
            # no changes, do nothing
            return CommitTableResponse(metadata=base_metadata, metadata_location=current_table.metadata_location)

        # write new metadata
        new_metadata_version = self._parse_metadata_version(current_table.metadata_location) + 1
        new_metadata_location = self._get_metadata_location(current_table.metadata.location, new_metadata_version)
        self._write_metadata(updated_metadata, current_table.io, new_metadata_location)

        # update table state
        current_table.metadata = updated_metadata

        return CommitTableResponse(metadata=updated_metadata, metadata_location=new_metadata_location)

    def load_table(self, identifier: Union[str, Identifier]) -> Table:
        try:
            identifier_tuple = Catalog.identifier_to_tuple(identifier)
            return self.__tables[identifier_tuple]
        except KeyError as error:
            raise NoSuchTableError(f"Table does not exist: {identifier_tuple}") from error

    def drop_table(self, identifier: Union[str, Identifier]) -> None:
        try:
            identifier_tuple = Catalog.identifier_to_tuple(identifier)
            self.__tables.pop(identifier_tuple)
        except KeyError as error:
            raise NoSuchTableError(f"Table does not exist: {identifier_tuple}") from error

    def purge_table(self, identifier: Union[str, Identifier]) -> None:
        self.drop_table(identifier)

    def rename_table(self, from_identifier: Union[str, Identifier], to_identifier: Union[str, Identifier]) -> Table:
        try:
            identifier_tuple = Catalog.identifier_to_tuple(from_identifier)
            table = self.__tables.pop(identifier_tuple)
        except KeyError as error:
            raise NoSuchTableError(f"Table does not exist: {identifier_tuple}") from error

        to_identifier = Catalog.identifier_to_tuple(to_identifier)
        to_namespace = Catalog.namespace_from(to_identifier)
        if to_namespace not in self.__namespaces:
            self.__namespaces[to_namespace] = {}

        self.__tables[to_identifier] = Table(
            identifier=to_identifier,
            metadata=table.metadata,
            metadata_location=table.metadata_location,
            io=self._load_file_io(properties=table.metadata.properties, location=table.metadata_location),
            catalog=self,
        )
        return self.__tables[to_identifier]

    def create_namespace(self, namespace: Union[str, Identifier], properties: Properties = EMPTY_DICT) -> None:
        namespace = Catalog.identifier_to_tuple(namespace)
        if namespace in self.__namespaces:
            raise NamespaceAlreadyExistsError(f"Namespace already exists: {namespace}")
        else:
            self.__namespaces[namespace] = properties if properties else {}

    def drop_namespace(self, namespace: Union[str, Identifier]) -> None:
        namespace = Catalog.identifier_to_tuple(namespace)
        if [table_identifier for table_identifier in self.__tables.keys() if namespace == table_identifier[:-1]]:
            raise NamespaceNotEmptyError(f"Namespace is not empty: {namespace}")
        try:
            self.__namespaces.pop(namespace)
        except KeyError as error:
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}") from error

    def list_tables(self, namespace: Optional[Union[str, Identifier]] = None) -> List[Identifier]:
        if namespace:
            namespace = Catalog.identifier_to_tuple(namespace)
            list_tables = [table_identifier for table_identifier in self.__tables.keys() if namespace == table_identifier[:-1]]
        else:
            list_tables = list(self.__tables.keys())

        return list_tables

    def list_namespaces(self, namespace: Union[str, Identifier] = ()) -> List[Identifier]:
        # Hierarchical namespace is not supported. Return an empty list
        if namespace:
            return []

        return list(self.__namespaces.keys())

    def load_namespace_properties(self, namespace: Union[str, Identifier]) -> Properties:
        namespace = Catalog.identifier_to_tuple(namespace)
        try:
            return self.__namespaces[namespace]
        except KeyError as error:
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}") from error

    def update_namespace_properties(
        self, namespace: Union[str, Identifier], removals: Optional[Set[str]] = None, updates: Properties = EMPTY_DICT
    ) -> PropertiesUpdateSummary:
        removed: Set[str] = set()
        updated: Set[str] = set()

        namespace = Catalog.identifier_to_tuple(namespace)
        if namespace in self.__namespaces:
            if removals:
                for key in removals:
                    if key in self.__namespaces[namespace]:
                        del self.__namespaces[namespace][key]
                        removed.add(key)
            if updates:
                for key, value in updates.items():
                    self.__namespaces[namespace][key] = value
                    updated.add(key)
        else:
            raise NoSuchNamespaceError(f"Namespace does not exist: {namespace}")

        expected_to_change = removed.difference(removals or set())

        return PropertiesUpdateSummary(
            removed=list(removed or []), updated=list(updates.keys() if updates else []), missing=list(expected_to_change)
        )

    def list_views(self, namespace: Optional[Union[str, Identifier]] = None) -> List[Identifier]:
        raise NotImplementedError

    def drop_view(self, identifier: Union[str, Identifier]) -> None:
        raise NotImplementedError

    def view_exists(self, identifier: Union[str, Identifier]) -> bool:
        raise NotImplementedError


@pytest.fixture
def catalog(tmp_path: PosixPath) -> InMemoryCatalog:
    return InMemoryCatalog("test.in_memory.catalog", **{WAREHOUSE: tmp_path.absolute().as_posix(), "test.key": "test.value"})


TEST_TABLE_IDENTIFIER = ("com", "organization", "department", "my_table")
TEST_TABLE_NAMESPACE = ("com", "organization", "department")
TEST_TABLE_NAME = "my_table"
TEST_TABLE_SCHEMA = Schema(
    NestedField(1, "x", LongType(), required=True),
    NestedField(2, "y", LongType(), doc="comment", required=True),
    NestedField(3, "z", LongType(), required=True),
)
TEST_TABLE_PARTITION_SPEC = PartitionSpec(PartitionField(name="x", transform=IdentityTransform(), source_id=1, field_id=1000))
TEST_TABLE_PROPERTIES = {"key1": "value1", "key2": "value2"}
NO_SUCH_TABLE_ERROR = "Table does not exist: \\('com', 'organization', 'department', 'my_table'\\)"
TABLE_ALREADY_EXISTS_ERROR = "Table already exists: \\('com', 'organization', 'department', 'my_table'\\)"
NAMESPACE_ALREADY_EXISTS_ERROR = "Namespace already exists: \\('com', 'organization', 'department'\\)"
NO_SUCH_NAMESPACE_ERROR = "Namespace does not exist: \\('com', 'organization', 'department'\\)"
NAMESPACE_NOT_EMPTY_ERROR = "Namespace is not empty: \\('com', 'organization', 'department'\\)"


def given_catalog_has_a_table(
    catalog: InMemoryCatalog,
    properties: Properties = EMPTY_DICT,
) -> Table:
    return catalog.create_table(
        identifier=TEST_TABLE_IDENTIFIER,
        schema=TEST_TABLE_SCHEMA,
        partition_spec=TEST_TABLE_PARTITION_SPEC,
        properties=properties or TEST_TABLE_PROPERTIES,
    )


def test_load_catalog_impl_not_full_path() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_catalog("catalog", **{"py-catalog-impl": "CustomCatalog"})

    assert "py-catalog-impl should be full path (module.CustomCatalog), got: CustomCatalog" in str(exc_info.value)


def test_load_catalog_impl_does_not_exist() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_catalog("catalog", **{"py-catalog-impl": "pyiceberg.does.not.exist.Catalog"})

    assert "Could not initialize Catalog: pyiceberg.does.not.exist.Catalog" in str(exc_info.value)


def test_load_catalog_has_type_and_impl() -> None:
    with pytest.raises(ValueError) as exc_info:
        load_catalog("catalog", **{"py-catalog-impl": "pyiceberg.does.not.exist.Catalog", "type": "sql"})

    assert (
        "Must not set both catalog type and py-catalog-impl configurations, "
        "but found type sql and py-catalog-impl pyiceberg.does.not.exist.Catalog" in str(exc_info.value)
    )


def test_namespace_from_tuple() -> None:
    # Given
    identifier = ("com", "organization", "department", "my_table")
    # When
    namespace_from = Catalog.namespace_from(identifier)
    # Then
    assert namespace_from == ("com", "organization", "department")


def test_namespace_from_str() -> None:
    # Given
    identifier = "com.organization.department.my_table"
    # When
    namespace_from = Catalog.namespace_from(identifier)
    # Then
    assert namespace_from == ("com", "organization", "department")


def test_name_from_tuple() -> None:
    # Given
    identifier = ("com", "organization", "department", "my_table")
    # When
    name_from = Catalog.table_name_from(identifier)
    # Then
    assert name_from == "my_table"


def test_name_from_str() -> None:
    # Given
    identifier = "com.organization.department.my_table"
    # When
    name_from = Catalog.table_name_from(identifier)
    # Then
    assert name_from == "my_table"


def test_create_table(catalog: InMemoryCatalog) -> None:
    table = catalog.create_table(
        identifier=TEST_TABLE_IDENTIFIER,
        schema=TEST_TABLE_SCHEMA,
        partition_spec=TEST_TABLE_PARTITION_SPEC,
        properties=TEST_TABLE_PROPERTIES,
    )
    assert catalog.load_table(TEST_TABLE_IDENTIFIER) == table


def test_create_table_location_override(catalog: InMemoryCatalog) -> None:
    new_location = f"{catalog._warehouse_location}/new_location"
    table = catalog.create_table(
        identifier=TEST_TABLE_IDENTIFIER,
        schema=TEST_TABLE_SCHEMA,
        location=new_location,
        partition_spec=TEST_TABLE_PARTITION_SPEC,
        properties=TEST_TABLE_PROPERTIES,
    )
    assert catalog.load_table(TEST_TABLE_IDENTIFIER) == table
    assert table.location() == new_location


def test_create_table_removes_trailing_slash_from_location(catalog: InMemoryCatalog) -> None:
    new_location = f"{catalog._warehouse_location}/new_location"
    table = catalog.create_table(
        identifier=TEST_TABLE_IDENTIFIER,
        schema=TEST_TABLE_SCHEMA,
        location=f"{new_location}/",
        partition_spec=TEST_TABLE_PARTITION_SPEC,
        properties=TEST_TABLE_PROPERTIES,
    )
    assert catalog.load_table(TEST_TABLE_IDENTIFIER) == table
    assert table.location() == new_location


@pytest.mark.parametrize(
    "schema,expected",
    [
        (lazy_fixture("pyarrow_schema_simple_without_ids"), lazy_fixture("iceberg_schema_simple_no_ids")),
        (lazy_fixture("iceberg_schema_simple"), lazy_fixture("iceberg_schema_simple")),
        (lazy_fixture("iceberg_schema_nested"), lazy_fixture("iceberg_schema_nested")),
        (lazy_fixture("pyarrow_schema_nested_without_ids"), lazy_fixture("iceberg_schema_nested_no_ids")),
    ],
)
def test_convert_schema_if_needed(
    schema: Union[Schema, pa.Schema],
    expected: Schema,
    catalog: InMemoryCatalog,
) -> None:
    assert expected == catalog._convert_schema_if_needed(schema)


def test_create_table_pyarrow_schema(catalog: InMemoryCatalog, pyarrow_schema_simple_without_ids: pa.Schema) -> None:
    table = catalog.create_table(
        identifier=TEST_TABLE_IDENTIFIER,
        schema=pyarrow_schema_simple_without_ids,
        properties=TEST_TABLE_PROPERTIES,
    )
    assert catalog.load_table(TEST_TABLE_IDENTIFIER) == table


def test_create_table_raises_error_when_table_already_exists(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)
    # When
    with pytest.raises(TableAlreadyExistsError, match=TABLE_ALREADY_EXISTS_ERROR):
        catalog.create_table(
            identifier=TEST_TABLE_IDENTIFIER,
            schema=TEST_TABLE_SCHEMA,
        )


def test_load_table(catalog: InMemoryCatalog) -> None:
    # Given
    given_table = given_catalog_has_a_table(catalog)
    # When
    table = catalog.load_table(TEST_TABLE_IDENTIFIER)
    # Then
    assert table == given_table


def test_load_table_from_self_identifier(catalog: InMemoryCatalog) -> None:
    # Given
    given_table = given_catalog_has_a_table(catalog)
    # When
    intermediate = catalog.load_table(TEST_TABLE_IDENTIFIER)
    table = catalog.load_table(intermediate._identifier)
    # Then
    assert table == given_table


def test_table_raises_error_on_table_not_found(catalog: InMemoryCatalog) -> None:
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(TEST_TABLE_IDENTIFIER)


def test_table_exists(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)
    # Then
    assert catalog.table_exists(TEST_TABLE_IDENTIFIER)


def test_table_exists_on_table_not_found(catalog: InMemoryCatalog) -> None:
    assert not catalog.table_exists(TEST_TABLE_IDENTIFIER)


def test_drop_table(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)
    # When
    catalog.drop_table(TEST_TABLE_IDENTIFIER)
    # Then
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(TEST_TABLE_IDENTIFIER)


def test_drop_table_from_self_identifier(catalog: InMemoryCatalog) -> None:
    # Given
    table = given_catalog_has_a_table(catalog)
    # When
    catalog.drop_table(table._identifier)
    # Then
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(table._identifier)
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(TEST_TABLE_IDENTIFIER)


def test_drop_table_that_does_not_exist_raise_error(catalog: InMemoryCatalog) -> None:
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(TEST_TABLE_IDENTIFIER)


def test_purge_table(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)
    # When
    catalog.purge_table(TEST_TABLE_IDENTIFIER)
    # Then
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(TEST_TABLE_IDENTIFIER)


def test_rename_table(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)

    # When
    new_table = "new.namespace.new_table"
    table = catalog.rename_table(TEST_TABLE_IDENTIFIER, new_table)

    # Then
    assert table._identifier == Catalog.identifier_to_tuple(new_table)

    # And
    table = catalog.load_table(new_table)
    assert table._identifier == Catalog.identifier_to_tuple(new_table)

    # And
    assert ("new", "namespace") in catalog.list_namespaces()

    # And
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(TEST_TABLE_IDENTIFIER)


def test_rename_table_from_self_identifier(catalog: InMemoryCatalog) -> None:
    # Given
    table = given_catalog_has_a_table(catalog)

    # When
    new_table_name = "new.namespace.new_table"
    new_table = catalog.rename_table(table._identifier, new_table_name)

    # Then
    assert new_table._identifier == Catalog.identifier_to_tuple(new_table_name)

    # And
    new_table = catalog.load_table(new_table._identifier)
    assert new_table._identifier == Catalog.identifier_to_tuple(new_table_name)

    # And
    assert ("new", "namespace") in catalog.list_namespaces()

    # And
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(table._identifier)
    with pytest.raises(NoSuchTableError, match=NO_SUCH_TABLE_ERROR):
        catalog.load_table(TEST_TABLE_IDENTIFIER)


def test_create_namespace(catalog: InMemoryCatalog) -> None:
    # When
    catalog.create_namespace(TEST_TABLE_NAMESPACE, TEST_TABLE_PROPERTIES)

    # Then
    assert TEST_TABLE_NAMESPACE in catalog.list_namespaces()
    assert TEST_TABLE_PROPERTIES == catalog.load_namespace_properties(TEST_TABLE_NAMESPACE)


def test_create_namespace_raises_error_on_existing_namespace(catalog: InMemoryCatalog) -> None:
    # Given
    catalog.create_namespace(TEST_TABLE_NAMESPACE, TEST_TABLE_PROPERTIES)
    # When
    with pytest.raises(NamespaceAlreadyExistsError, match=NAMESPACE_ALREADY_EXISTS_ERROR):
        catalog.create_namespace(TEST_TABLE_NAMESPACE, TEST_TABLE_PROPERTIES)


def test_get_namespace_metadata_raises_error_when_namespace_does_not_exist(catalog: InMemoryCatalog) -> None:
    with pytest.raises(NoSuchNamespaceError, match=NO_SUCH_NAMESPACE_ERROR):
        catalog.load_namespace_properties(TEST_TABLE_NAMESPACE)


def test_list_namespaces(catalog: InMemoryCatalog) -> None:
    # Given
    catalog.create_namespace(TEST_TABLE_NAMESPACE, TEST_TABLE_PROPERTIES)
    # When
    namespaces = catalog.list_namespaces()
    # Then
    assert TEST_TABLE_NAMESPACE in namespaces


def test_drop_namespace(catalog: InMemoryCatalog) -> None:
    # Given
    catalog.create_namespace(TEST_TABLE_NAMESPACE, TEST_TABLE_PROPERTIES)
    # When
    catalog.drop_namespace(TEST_TABLE_NAMESPACE)
    # Then
    assert TEST_TABLE_NAMESPACE not in catalog.list_namespaces()


def test_drop_namespace_raises_error_when_namespace_does_not_exist(catalog: InMemoryCatalog) -> None:
    with pytest.raises(NoSuchNamespaceError, match=NO_SUCH_NAMESPACE_ERROR):
        catalog.drop_namespace(TEST_TABLE_NAMESPACE)


def test_drop_namespace_raises_error_when_namespace_not_empty(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)
    # When
    with pytest.raises(NamespaceNotEmptyError, match=NAMESPACE_NOT_EMPTY_ERROR):
        catalog.drop_namespace(TEST_TABLE_NAMESPACE)


def test_list_tables(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)
    # When
    tables = catalog.list_tables()
    # Then
    assert tables
    assert TEST_TABLE_IDENTIFIER in tables


def test_list_tables_under_a_namespace(catalog: InMemoryCatalog) -> None:
    # Given
    given_catalog_has_a_table(catalog)
    new_namespace = ("new", "namespace")
    catalog.create_namespace(new_namespace)
    # When
    all_tables = catalog.list_tables()
    new_namespace_tables = catalog.list_tables(new_namespace)
    # Then
    assert all_tables
    assert TEST_TABLE_IDENTIFIER in all_tables
    assert new_namespace_tables == []


def test_update_namespace_metadata(catalog: InMemoryCatalog) -> None:
    # Given
    catalog.create_namespace(TEST_TABLE_NAMESPACE, TEST_TABLE_PROPERTIES)

    # When
    new_metadata = {"key3": "value3", "key4": "value4"}
    summary = catalog.update_namespace_properties(TEST_TABLE_NAMESPACE, updates=new_metadata)

    # Then
    assert TEST_TABLE_NAMESPACE in catalog.list_namespaces()
    assert new_metadata.items() <= catalog.load_namespace_properties(TEST_TABLE_NAMESPACE).items()
    assert summary == PropertiesUpdateSummary(removed=[], updated=["key3", "key4"], missing=[])


def test_update_namespace_metadata_removals(catalog: InMemoryCatalog) -> None:
    # Given
    catalog.create_namespace(TEST_TABLE_NAMESPACE, TEST_TABLE_PROPERTIES)

    # When
    new_metadata = {"key3": "value3", "key4": "value4"}
    remove_metadata = {"key1"}
    summary = catalog.update_namespace_properties(TEST_TABLE_NAMESPACE, remove_metadata, new_metadata)

    # Then
    assert TEST_TABLE_NAMESPACE in catalog.list_namespaces()
    assert new_metadata.items() <= catalog.load_namespace_properties(TEST_TABLE_NAMESPACE).items()
    assert remove_metadata.isdisjoint(catalog.load_namespace_properties(TEST_TABLE_NAMESPACE).keys())
    assert summary == PropertiesUpdateSummary(removed=["key1"], updated=["key3", "key4"], missing=[])


def test_update_namespace_metadata_raises_error_when_namespace_does_not_exist(catalog: InMemoryCatalog) -> None:
    with pytest.raises(NoSuchNamespaceError, match=NO_SUCH_NAMESPACE_ERROR):
        catalog.update_namespace_properties(TEST_TABLE_NAMESPACE, updates=TEST_TABLE_PROPERTIES)


def test_commit_table(catalog: InMemoryCatalog) -> None:
    # Given
    given_table = given_catalog_has_a_table(catalog)
    new_schema = Schema(
        NestedField(1, "x", LongType()),
        NestedField(2, "y", LongType(), doc="comment"),
        NestedField(3, "z", LongType()),
        NestedField(4, "add", LongType()),
    )

    # When
    response = given_table.catalog.commit_table(
        given_table,
        updates=(
            AddSchemaUpdate(schema=new_schema, last_column_id=new_schema.highest_field_id),
            SetCurrentSchemaUpdate(schema_id=-1),
        ),
        requirements=(),
    )

    # Then
    assert response.metadata.table_uuid == given_table.metadata.table_uuid
    assert len(response.metadata.schemas) == 2
    assert response.metadata.schemas[1] == new_schema
    assert response.metadata.current_schema_id == new_schema.schema_id


def test_add_column(catalog: InMemoryCatalog) -> None:
    given_table = given_catalog_has_a_table(catalog)

    given_table.update_schema().add_column(path="new_column1", field_type=IntegerType()).commit()

    assert given_table.schema() == Schema(
        NestedField(field_id=1, name="x", field_type=LongType(), required=True),
        NestedField(field_id=2, name="y", field_type=LongType(), required=True, doc="comment"),
        NestedField(field_id=3, name="z", field_type=LongType(), required=True),
        NestedField(field_id=4, name="new_column1", field_type=IntegerType(), required=False),
        identifier_field_ids=[],
    )
    assert given_table.schema().schema_id == 1

    transaction = given_table.transaction()
    transaction.update_schema().add_column(path="new_column2", field_type=IntegerType(), doc="doc").commit()
    transaction.commit_transaction()

    assert given_table.schema() == Schema(
        NestedField(field_id=1, name="x", field_type=LongType(), required=True),
        NestedField(field_id=2, name="y", field_type=LongType(), required=True, doc="comment"),
        NestedField(field_id=3, name="z", field_type=LongType(), required=True),
        NestedField(field_id=4, name="new_column1", field_type=IntegerType(), required=False),
        NestedField(field_id=5, name="new_column2", field_type=IntegerType(), required=False, doc="doc"),
        identifier_field_ids=[],
    )
    assert given_table.schema().schema_id == 2


def test_add_column_with_statement(catalog: InMemoryCatalog) -> None:
    given_table = given_catalog_has_a_table(catalog)

    with given_table.update_schema() as tx:
        tx.add_column(path="new_column1", field_type=IntegerType())

    assert given_table.schema() == Schema(
        NestedField(field_id=1, name="x", field_type=LongType(), required=True),
        NestedField(field_id=2, name="y", field_type=LongType(), required=True, doc="comment"),
        NestedField(field_id=3, name="z", field_type=LongType(), required=True),
        NestedField(field_id=4, name="new_column1", field_type=IntegerType(), required=False),
        identifier_field_ids=[],
    )
    assert given_table.schema().schema_id == 1

    with given_table.transaction() as tx:
        tx.update_schema().add_column(path="new_column2", field_type=IntegerType(), doc="doc").commit()

    assert given_table.schema() == Schema(
        NestedField(field_id=1, name="x", field_type=LongType(), required=True),
        NestedField(field_id=2, name="y", field_type=LongType(), required=True, doc="comment"),
        NestedField(field_id=3, name="z", field_type=LongType(), required=True),
        NestedField(field_id=4, name="new_column1", field_type=IntegerType(), required=False),
        NestedField(field_id=5, name="new_column2", field_type=IntegerType(), required=False, doc="doc"),
        identifier_field_ids=[],
    )
    assert given_table.schema().schema_id == 2


def test_catalog_repr(catalog: InMemoryCatalog) -> None:
    s = repr(catalog)
    assert s == "test.in_memory.catalog (<class 'test_base.InMemoryCatalog'>)"


def test_table_properties_int_value(catalog: InMemoryCatalog) -> None:
    # table properties can be set to int, but still serialized to string
    property_with_int = {"property_name": 42}
    given_table = given_catalog_has_a_table(catalog, properties=property_with_int)
    assert isinstance(given_table.properties["property_name"], str)


def test_table_properties_raise_for_none_value(catalog: InMemoryCatalog) -> None:
    property_with_none = {"property_name": None}
    with pytest.raises(ValidationError) as exc_info:
        _ = given_catalog_has_a_table(catalog, properties=property_with_none)
    assert "None type is not a supported value in properties: property_name" in str(exc_info.value)
