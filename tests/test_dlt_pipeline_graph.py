"""Static proof that the DLT pipeline code wires together as a valid dataset
graph, without Spark, a cluster, or a Databricks workspace.

`pipeline/` can't actually run here (it needs a real DLT runtime — see its own
docstrings), but a whole class of deploy-time failures doesn't need Spark to
catch: two datasets with the same name in one pipeline, a `dlt.read` of a
table no pipeline defines, an agent-facing Gold table that never gets
published to the `gold` schema, or a hard-stop expectation that is filtered
around before it can fire. This file loads every library file listed in
`resources/dlt_pipeline.yml` against a recording fake `dlt` module (and
MagicMock stand-ins for pyspark), calls every dataset function once so its
`dlt.read`/`dlt.read_stream` calls are recorded, and asserts on the resulting
graph.

What this does NOT prove: that the Spark transformations themselves are
correct, that column names line up at runtime, or that Unity Catalog accepts
the masks — only a real pipeline run can show that.
"""

import runpy
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.tools.data_query import _ALLOWED_TABLES
from common.contracts import MASKED_COLUMNS

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLD_SCHEMA = "healthcare_agentic_de.gold"


@dataclass
class Dataset:
    name: str
    kind: str  # "table" | "view" | "streaming_table"
    fn: object = None
    expectations: dict = field(default_factory=dict)  # name -> "warn" | "drop" | "fail"
    reads: list = field(default_factory=list)
    read_frames: dict = field(default_factory=dict)  # source name -> MagicMock frame returned to fn


class FakeDlt(types.ModuleType):
    """Records dataset definitions and reads instead of building a real DLT graph."""

    def __init__(self):
        super().__init__("dlt")
        self.datasets: dict[str, Dataset] = {}
        self.duplicates: list[str] = []
        self._current: Dataset | None = None

    def _register(self, ds: Dataset) -> None:
        if ds.name in self.datasets:
            self.duplicates.append(ds.name)
        self.datasets[ds.name] = ds

    def _define(self, kind, name=None, **_kwargs):
        def decorator(fn):
            ds = Dataset(name=name or fn.__name__, kind=kind, fn=fn)
            ds.expectations.update(getattr(fn, "_expectations", {}))
            self._register(ds)
            return fn

        return decorator

    def table(self, name=None, **kwargs):
        return self._define("table", name, **kwargs)

    def view(self, name=None, **kwargs):
        return self._define("view", name, **kwargs)

    def _expect(self, action):
        def factory(name, _constraint):
            def decorator(fn):
                fn._expectations = {**getattr(fn, "_expectations", {}), name: action}
                return fn

            return decorator

        return factory

    @property
    def expect(self):
        return self._expect("warn")

    @property
    def expect_or_drop(self):
        return self._expect("drop")

    @property
    def expect_or_fail(self):
        return self._expect("fail")

    def create_streaming_table(self, name, **_kwargs):
        self._register(Dataset(name=name, kind="streaming_table"))

    def apply_changes(self, target, source, **_kwargs):
        self.datasets.setdefault(target, Dataset(name=target, kind="missing")).reads.append(source)

    def _read(self, name):
        frame = MagicMock(name=f"frame:{name}")
        if self._current is not None:
            self._current.reads.append(name)
            self._current.read_frames[name] = frame
        return frame

    def read(self, name):
        return self._read(name)

    def read_stream(self, name):
        return self._read(name)


class _FakeConf:
    def get(self, _key, default=None):
        return default


class _AnyColumn:
    """Stand-in for a pyspark Column: every call, attribute, and operator
    returns another column, so expressions like `(F.col("v") < 55) | ...`
    evaluate without Spark."""

    def __getattr__(self, _name):
        return self

    def __call__(self, *_args, **_kwargs):
        return self

    def _op(self, *_args):
        return self

    __lt__ = __le__ = __gt__ = __ge__ = __eq__ = __ne__ = _op
    __and__ = __or__ = __invert__ = __add__ = __sub__ = __mul__ = __truediv__ = _op
    __hash__ = object.__hash__


class _FakeFunctionsModule(types.ModuleType):
    def __getattr__(self, _name):
        return _AnyColumn()


def _fake_pyspark_modules() -> dict[str, types.ModuleType]:
    spark = MagicMock(name="spark")
    spark.conf = _FakeConf()
    sql = MagicMock(name="pyspark.sql")
    sql.SparkSession.getActiveSession.return_value = spark
    # `from pyspark.sql import functions as F` resolves the attribute on the
    # parent module first, so the fake has to live there too.
    sql.functions = _FakeFunctionsModule("pyspark.sql.functions")
    return {
        "pyspark": MagicMock(name="pyspark"),
        "pyspark.sql": sql,
        "pyspark.sql.functions": sql.functions,
        "pyspark.sql.types": MagicMock(name="pyspark.sql.types"),
    }


def _pipeline_libraries() -> dict[str, list[Path]]:
    bundle = yaml.safe_load((REPO_ROOT / "resources" / "dlt_pipeline.yml").read_text())
    libraries = {}
    for key, pipeline in bundle["resources"]["pipelines"].items():
        libraries[key] = [
            (REPO_ROOT / "resources" / lib["file"]["path"]).resolve() for lib in pipeline["libraries"]
        ]
    return libraries


def _load_pipeline(paths: list[Path], monkeypatch) -> FakeDlt:
    fake_dlt = FakeDlt()
    # Pipeline files append bundle.sourcePath to sys.path; keep that local.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "dlt", fake_dlt)
    for name, module in _fake_pyspark_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    # pipeline.common.schemas imports pyspark.sql.types at module level; make
    # sure it's re-imported against the fakes and not left cached afterwards.
    monkeypatch.delitem(sys.modules, "pipeline.common.schemas", raising=False)

    for path in paths:
        runpy.run_path(str(path), run_name=f"dlt_lib_{path.stem}")

    for ds in list(fake_dlt.datasets.values()):
        if ds.fn is not None:
            fake_dlt._current = ds
            ds.fn()
            fake_dlt._current = None
    monkeypatch.delitem(sys.modules, "pipeline.common.schemas", raising=False)
    return fake_dlt


@pytest.fixture
def pipelines(monkeypatch) -> dict[str, FakeDlt]:
    return {key: _load_pipeline(paths, monkeypatch) for key, paths in _pipeline_libraries().items()}


def test_every_bundle_library_path_exists():
    for key, paths in _pipeline_libraries().items():
        for path in paths:
            assert path.is_file(), f"{key}: library {path} does not exist"


def test_no_dataset_is_defined_twice_in_one_pipeline(pipelines):
    for key, graph in pipelines.items():
        assert graph.duplicates == [], f"{key} defines {graph.duplicates} more than once"


def test_every_read_resolves_to_a_dataset_in_the_same_pipeline(pipelines):
    for key, graph in pipelines.items():
        for ds in graph.datasets.values():
            assert ds.kind != "missing", f"{key}: apply_changes target {ds.name} has no create_streaming_table"
            for source in ds.reads:
                assert source in graph.datasets, f"{key}: {ds.name} reads {source}, which isn't defined"
                assert source != ds.name, f"{key}: {ds.name} reads itself"


def test_every_agent_queryable_table_is_published_to_the_gold_schema(pipelines):
    published = {name for graph in pipelines.values() for name in graph.datasets}
    for table in _ALLOWED_TABLES:
        assert f"{GOLD_SCHEMA}.{table}" in published, f"{table} is never published to {GOLD_SCHEMA}"


def test_masked_tables_are_published_to_the_gold_schema(pipelines):
    published = {name for graph in pipelines.values() for name in graph.datasets}
    for table in MASKED_COLUMNS:
        assert f"{GOLD_SCHEMA}.{table}" in published


def test_known_event_type_hard_stop_sees_unfiltered_events(pipelines):
    """`known_event_type` is expect_or_fail. If the dataset carrying it
    filters its input by event_type first, an unknown type is removed before
    the expectation ever evaluates and the hard stop can never fire."""
    carriers = [
        ds
        for graph in pipelines.values()
        for ds in graph.datasets.values()
        if ds.expectations.get("known_event_type") == "fail"
    ]
    assert len(carriers) == 1, "exactly one dataset should carry the known_event_type hard stop"
    carrier = carriers[0]
    assert carrier.reads == ["bronze_patient_events"]
    frame = carrier.read_frames["bronze_patient_events"]
    assert not frame.filter.called and not frame.where.called, (
        f"{carrier.name} filters bronze_patient_events before known_event_type can see unknown types"
    )


def test_encounter_and_vitals_consumers_read_the_contract_checked_stream(pipelines):
    """Every Silver consumer of patient events must sit downstream of the
    hard-stop view, or an unknown event type could reach it anyway."""
    graph = pipelines["streaming_patient_pipeline"]
    carrier = next(ds.name for ds in graph.datasets.values() if "known_event_type" in ds.expectations)
    direct_bronze_readers = {
        ds.name for ds in graph.datasets.values() if "bronze_patient_events" in ds.reads
    }
    assert direct_bronze_readers == {carrier}
