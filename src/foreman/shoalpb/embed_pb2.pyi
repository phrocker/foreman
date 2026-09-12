from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper

DESCRIPTOR: _descriptor.FileDescriptor

class TableWorkload(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TABLE_WORKLOAD_UNSPECIFIED: _ClassVar[TableWorkload]
    TABLE_WORKLOAD_OPERATIONAL: _ClassVar[TableWorkload]
    TABLE_WORKLOAD_ANALYTICAL: _ClassVar[TableWorkload]

class TableFileFormat(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TABLE_FILE_FORMAT_UNSPECIFIED: _ClassVar[TableFileFormat]
    TABLE_FILE_FORMAT_RFILE: _ClassVar[TableFileFormat]
    TABLE_FILE_FORMAT_PARQUET: _ClassVar[TableFileFormat]

class MutationStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MUTATION_STATUS_UNSPECIFIED: _ClassVar[MutationStatus]
    MUTATION_STATUS_ACCEPTED: _ClassVar[MutationStatus]
    MUTATION_STATUS_REJECTED: _ClassVar[MutationStatus]
TABLE_WORKLOAD_UNSPECIFIED: TableWorkload
TABLE_WORKLOAD_OPERATIONAL: TableWorkload
TABLE_WORKLOAD_ANALYTICAL: TableWorkload
TABLE_FILE_FORMAT_UNSPECIFIED: TableFileFormat
TABLE_FILE_FORMAT_RFILE: TableFileFormat
TABLE_FILE_FORMAT_PARQUET: TableFileFormat
MUTATION_STATUS_UNSPECIFIED: MutationStatus
MUTATION_STATUS_ACCEPTED: MutationStatus
MUTATION_STATUS_REJECTED: MutationStatus

class CreateTableRequest(_message.Message):
    __slots__ = ("table", "splits", "workload", "file_format", "default_embedding")
    TABLE_FIELD_NUMBER: _ClassVar[int]
    SPLITS_FIELD_NUMBER: _ClassVar[int]
    WORKLOAD_FIELD_NUMBER: _ClassVar[int]
    FILE_FORMAT_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_EMBEDDING_FIELD_NUMBER: _ClassVar[int]
    table: str
    splits: _containers.RepeatedScalarFieldContainer[str]
    workload: TableWorkload
    file_format: TableFileFormat
    default_embedding: str
    def __init__(self, table: str | None = ..., splits: _Iterable[str] | None = ..., workload: TableWorkload | str | None = ..., file_format: TableFileFormat | str | None = ..., default_embedding: str | None = ...) -> None: ...

class CreateTableResponse(_message.Message):
    __slots__ = ("table", "tablets", "workload", "file_format")
    TABLE_FIELD_NUMBER: _ClassVar[int]
    TABLETS_FIELD_NUMBER: _ClassVar[int]
    WORKLOAD_FIELD_NUMBER: _ClassVar[int]
    FILE_FORMAT_FIELD_NUMBER: _ClassVar[int]
    table: str
    tablets: int
    workload: TableWorkload
    file_format: TableFileFormat
    def __init__(self, table: str | None = ..., tablets: int | None = ..., workload: TableWorkload | str | None = ..., file_format: TableFileFormat | str | None = ...) -> None: ...

class Mutation(_message.Message):
    __slots__ = ("row", "entries", "conditions")
    ROW_FIELD_NUMBER: _ClassVar[int]
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    CONDITIONS_FIELD_NUMBER: _ClassVar[int]
    row: bytes
    entries: _containers.RepeatedCompositeFieldContainer[Entry]
    conditions: _containers.RepeatedCompositeFieldContainer[Condition]
    def __init__(self, row: bytes | None = ..., entries: _Iterable[Entry | _Mapping] | None = ..., conditions: _Iterable[Condition | _Mapping] | None = ...) -> None: ...

class Entry(_message.Message):
    __slots__ = ("column_family", "column_qualifier", "column_visibility", "timestamp", "value", "delete")
    COLUMN_FAMILY_FIELD_NUMBER: _ClassVar[int]
    COLUMN_QUALIFIER_FIELD_NUMBER: _ClassVar[int]
    COLUMN_VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    DELETE_FIELD_NUMBER: _ClassVar[int]
    column_family: bytes
    column_qualifier: bytes
    column_visibility: bytes
    timestamp: int
    value: bytes
    delete: bool
    def __init__(self, column_family: bytes | None = ..., column_qualifier: bytes | None = ..., column_visibility: bytes | None = ..., timestamp: int | None = ..., value: bytes | None = ..., delete: bool | None = ...) -> None: ...

class Condition(_message.Message):
    __slots__ = ("column_family", "column_qualifier", "column_visibility", "timestamp", "absent", "value_equals")
    COLUMN_FAMILY_FIELD_NUMBER: _ClassVar[int]
    COLUMN_QUALIFIER_FIELD_NUMBER: _ClassVar[int]
    COLUMN_VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    ABSENT_FIELD_NUMBER: _ClassVar[int]
    VALUE_EQUALS_FIELD_NUMBER: _ClassVar[int]
    column_family: bytes
    column_qualifier: bytes
    column_visibility: bytes
    timestamp: int
    absent: bool
    value_equals: bytes
    def __init__(self, column_family: bytes | None = ..., column_qualifier: bytes | None = ..., column_visibility: bytes | None = ..., timestamp: int | None = ..., absent: bool | None = ..., value_equals: bytes | None = ...) -> None: ...

class WriteRequest(_message.Message):
    __slots__ = ("table", "mutations")
    TABLE_FIELD_NUMBER: _ClassVar[int]
    MUTATIONS_FIELD_NUMBER: _ClassVar[int]
    table: str
    mutations: _containers.RepeatedCompositeFieldContainer[Mutation]
    def __init__(self, table: str | None = ..., mutations: _Iterable[Mutation | _Mapping] | None = ...) -> None: ...

class MutationResult(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: MutationStatus
    def __init__(self, status: MutationStatus | str | None = ...) -> None: ...

class WriteResponse(_message.Message):
    __slots__ = ("written", "results")
    WRITTEN_FIELD_NUMBER: _ClassVar[int]
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    written: int
    results: _containers.RepeatedCompositeFieldContainer[MutationResult]
    def __init__(self, written: int | None = ..., results: _Iterable[MutationResult | _Mapping] | None = ...) -> None: ...

class ScanRequest(_message.Message):
    __slots__ = ("table", "row_prefix", "start_row", "start_inclusive", "end_row", "end_inclusive", "limit", "batch_size", "term_filter", "vector_search", "edge_expand", "score_filter", "as_of")
    TABLE_FIELD_NUMBER: _ClassVar[int]
    ROW_PREFIX_FIELD_NUMBER: _ClassVar[int]
    START_ROW_FIELD_NUMBER: _ClassVar[int]
    START_INCLUSIVE_FIELD_NUMBER: _ClassVar[int]
    END_ROW_FIELD_NUMBER: _ClassVar[int]
    END_INCLUSIVE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    BATCH_SIZE_FIELD_NUMBER: _ClassVar[int]
    TERM_FILTER_FIELD_NUMBER: _ClassVar[int]
    VECTOR_SEARCH_FIELD_NUMBER: _ClassVar[int]
    EDGE_EXPAND_FIELD_NUMBER: _ClassVar[int]
    SCORE_FILTER_FIELD_NUMBER: _ClassVar[int]
    AS_OF_FIELD_NUMBER: _ClassVar[int]
    table: str
    row_prefix: str
    start_row: bytes
    start_inclusive: bool
    end_row: bytes
    end_inclusive: bool
    limit: int
    batch_size: int
    term_filter: TermFilter
    vector_search: VectorSearch
    edge_expand: EdgeExpand
    score_filter: ScoreFilter
    as_of: int
    def __init__(self, table: str | None = ..., row_prefix: str | None = ..., start_row: bytes | None = ..., start_inclusive: bool | None = ..., end_row: bytes | None = ..., end_inclusive: bool | None = ..., limit: int | None = ..., batch_size: int | None = ..., term_filter: TermFilter | _Mapping | None = ..., vector_search: VectorSearch | _Mapping | None = ..., edge_expand: EdgeExpand | _Mapping | None = ..., score_filter: ScoreFilter | _Mapping | None = ..., as_of: int | None = ...) -> None: ...

class TermFilter(_message.Message):
    __slots__ = ("term_rows", "primary_prefix", "id_source", "posting_cf", "phrase", "numeric_range")
    TERM_ROWS_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_PREFIX_FIELD_NUMBER: _ClassVar[int]
    ID_SOURCE_FIELD_NUMBER: _ClassVar[int]
    POSTING_CF_FIELD_NUMBER: _ClassVar[int]
    PHRASE_FIELD_NUMBER: _ClassVar[int]
    NUMERIC_RANGE_FIELD_NUMBER: _ClassVar[int]
    term_rows: _containers.RepeatedScalarFieldContainer[bytes]
    primary_prefix: bytes
    id_source: str
    posting_cf: bytes
    phrase: bool
    numeric_range: NumericRange
    def __init__(self, term_rows: _Iterable[bytes] | None = ..., primary_prefix: bytes | None = ..., id_source: str | None = ..., posting_cf: bytes | None = ..., phrase: bool | None = ..., numeric_range: NumericRange | _Mapping | None = ...) -> None: ...

class NumericRange(_message.Message):
    __slots__ = ("lower", "lower_set", "upper", "upper_set", "lower_inclusive", "upper_inclusive")
    LOWER_FIELD_NUMBER: _ClassVar[int]
    LOWER_SET_FIELD_NUMBER: _ClassVar[int]
    UPPER_FIELD_NUMBER: _ClassVar[int]
    UPPER_SET_FIELD_NUMBER: _ClassVar[int]
    LOWER_INCLUSIVE_FIELD_NUMBER: _ClassVar[int]
    UPPER_INCLUSIVE_FIELD_NUMBER: _ClassVar[int]
    lower: float
    lower_set: bool
    upper: float
    upper_set: bool
    lower_inclusive: bool
    upper_inclusive: bool
    def __init__(self, lower: float | None = ..., lower_set: bool | None = ..., upper: float | None = ..., upper_set: bool | None = ..., lower_inclusive: bool | None = ..., upper_inclusive: bool | None = ...) -> None: ...

class VectorSearch(_message.Message):
    __slots__ = ("query", "top_k", "embedding_cf", "metric", "min_score", "min_score_set", "embedding_space")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    EMBEDDING_CF_FIELD_NUMBER: _ClassVar[int]
    METRIC_FIELD_NUMBER: _ClassVar[int]
    MIN_SCORE_FIELD_NUMBER: _ClassVar[int]
    MIN_SCORE_SET_FIELD_NUMBER: _ClassVar[int]
    EMBEDDING_SPACE_FIELD_NUMBER: _ClassVar[int]
    query: bytes
    top_k: int
    embedding_cf: bytes
    metric: str
    min_score: float
    min_score_set: bool
    embedding_space: str
    def __init__(self, query: bytes | None = ..., top_k: int | None = ..., embedding_cf: bytes | None = ..., metric: str | None = ..., min_score: float | None = ..., min_score_set: bool | None = ..., embedding_space: str | None = ...) -> None: ...

class ScoreFilter(_message.Message):
    __slots__ = ("score_cf", "method", "query", "top_k", "params", "timestamp_anchor_ms", "half_life_ms")
    SCORE_CF_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    QUERY_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_ANCHOR_MS_FIELD_NUMBER: _ClassVar[int]
    HALF_LIFE_MS_FIELD_NUMBER: _ClassVar[int]
    score_cf: bytes
    method: str
    query: bytes
    top_k: int
    params: _containers.RepeatedScalarFieldContainer[float]
    timestamp_anchor_ms: int
    half_life_ms: int
    def __init__(self, score_cf: bytes | None = ..., method: str | None = ..., query: bytes | None = ..., top_k: int | None = ..., params: _Iterable[float] | None = ..., timestamp_anchor_ms: int | None = ..., half_life_ms: int | None = ...) -> None: ...

class EdgeExpand(_message.Message):
    __slots__ = ("anchor_rows", "edge_cf", "edge_field", "field_sep", "id_index", "id_index_set", "rel_index", "relationships", "primary_prefix", "include_anchors", "max_hops", "edge_weights")
    ANCHOR_ROWS_FIELD_NUMBER: _ClassVar[int]
    EDGE_CF_FIELD_NUMBER: _ClassVar[int]
    EDGE_FIELD_FIELD_NUMBER: _ClassVar[int]
    FIELD_SEP_FIELD_NUMBER: _ClassVar[int]
    ID_INDEX_FIELD_NUMBER: _ClassVar[int]
    ID_INDEX_SET_FIELD_NUMBER: _ClassVar[int]
    REL_INDEX_FIELD_NUMBER: _ClassVar[int]
    RELATIONSHIPS_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_PREFIX_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_ANCHORS_FIELD_NUMBER: _ClassVar[int]
    MAX_HOPS_FIELD_NUMBER: _ClassVar[int]
    EDGE_WEIGHTS_FIELD_NUMBER: _ClassVar[int]
    anchor_rows: _containers.RepeatedScalarFieldContainer[bytes]
    edge_cf: bytes
    edge_field: str
    field_sep: bytes
    id_index: int
    id_index_set: bool
    rel_index: int
    relationships: _containers.RepeatedScalarFieldContainer[str]
    primary_prefix: bytes
    include_anchors: bool
    max_hops: int
    edge_weights: _containers.RepeatedCompositeFieldContainer[EdgeWeight]
    def __init__(self, anchor_rows: _Iterable[bytes] | None = ..., edge_cf: bytes | None = ..., edge_field: str | None = ..., field_sep: bytes | None = ..., id_index: int | None = ..., id_index_set: bool | None = ..., rel_index: int | None = ..., relationships: _Iterable[str] | None = ..., primary_prefix: bytes | None = ..., include_anchors: bool | None = ..., max_hops: int | None = ..., edge_weights: _Iterable[EdgeWeight | _Mapping] | None = ...) -> None: ...

class EdgeWeight(_message.Message):
    __slots__ = ("relationship", "weight")
    RELATIONSHIP_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    relationship: str
    weight: float
    def __init__(self, relationship: str | None = ..., weight: float | None = ...) -> None: ...

class Cell(_message.Message):
    __slots__ = ("row", "column_family", "column_qualifier", "column_visibility", "timestamp", "value")
    ROW_FIELD_NUMBER: _ClassVar[int]
    COLUMN_FAMILY_FIELD_NUMBER: _ClassVar[int]
    COLUMN_QUALIFIER_FIELD_NUMBER: _ClassVar[int]
    COLUMN_VISIBILITY_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    row: bytes
    column_family: bytes
    column_qualifier: bytes
    column_visibility: bytes
    timestamp: int
    value: bytes
    def __init__(self, row: bytes | None = ..., column_family: bytes | None = ..., column_qualifier: bytes | None = ..., column_visibility: bytes | None = ..., timestamp: int | None = ..., value: bytes | None = ...) -> None: ...

class ScanResponse(_message.Message):
    __slots__ = ("cells",)
    CELLS_FIELD_NUMBER: _ClassVar[int]
    cells: _containers.RepeatedCompositeFieldContainer[Cell]
    def __init__(self, cells: _Iterable[Cell | _Mapping] | None = ...) -> None: ...

class FlushRequest(_message.Message):
    __slots__ = ("table",)
    TABLE_FIELD_NUMBER: _ClassVar[int]
    table: str
    def __init__(self, table: str | None = ...) -> None: ...

class FlushResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class CompactRequest(_message.Message):
    __slots__ = ("table", "workload", "file_format")
    TABLE_FIELD_NUMBER: _ClassVar[int]
    WORKLOAD_FIELD_NUMBER: _ClassVar[int]
    FILE_FORMAT_FIELD_NUMBER: _ClassVar[int]
    table: str
    workload: TableWorkload
    file_format: TableFileFormat
    def __init__(self, table: str | None = ..., workload: TableWorkload | str | None = ..., file_format: TableFileFormat | str | None = ...) -> None: ...

class CompactResponse(_message.Message):
    __slots__ = ("table", "workload", "file_format")
    TABLE_FIELD_NUMBER: _ClassVar[int]
    WORKLOAD_FIELD_NUMBER: _ClassVar[int]
    FILE_FORMAT_FIELD_NUMBER: _ClassVar[int]
    table: str
    workload: TableWorkload
    file_format: TableFileFormat
    def __init__(self, table: str | None = ..., workload: TableWorkload | str | None = ..., file_format: TableFileFormat | str | None = ...) -> None: ...

class StatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class TableStatus(_message.Message):
    __slots__ = ("table", "tablets", "immutable_files", "workload", "file_format")
    TABLE_FIELD_NUMBER: _ClassVar[int]
    TABLETS_FIELD_NUMBER: _ClassVar[int]
    IMMUTABLE_FILES_FIELD_NUMBER: _ClassVar[int]
    WORKLOAD_FIELD_NUMBER: _ClassVar[int]
    FILE_FORMAT_FIELD_NUMBER: _ClassVar[int]
    table: str
    tablets: int
    immutable_files: int
    workload: TableWorkload
    file_format: TableFileFormat
    def __init__(self, table: str | None = ..., tablets: int | None = ..., immutable_files: int | None = ..., workload: TableWorkload | str | None = ..., file_format: TableFileFormat | str | None = ...) -> None: ...

class StatusResponse(_message.Message):
    __slots__ = ("tables", "table_statuses")
    TABLES_FIELD_NUMBER: _ClassVar[int]
    TABLE_STATUSES_FIELD_NUMBER: _ClassVar[int]
    tables: _containers.RepeatedScalarFieldContainer[str]
    table_statuses: _containers.RepeatedCompositeFieldContainer[TableStatus]
    def __init__(self, tables: _Iterable[str] | None = ..., table_statuses: _Iterable[TableStatus | _Mapping] | None = ...) -> None: ...
