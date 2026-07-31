from nan_fung import datasources
from nan_fung.datasources.common import source_result


def test_source_result_has_agent_friendly_envelope() -> None:
    result = source_result(
        category="example",
        source="Example Source",
        source_url="https://example.com/data",
        published_at="2026-07-31",
        records=[{"value": 1}],
    )

    assert result["category"] == "example"
    assert result["published_at"] == "2026-07-31"
    assert result["source_updated_at"] is None
    assert result["records"] == [{"value": 1}]
    assert result["retrieved_at"].endswith("+00:00")


def test_datasource_functions_are_exported() -> None:
    assert len(datasources.__all__) == 16
    assert all(callable(getattr(datasources, name)) for name in datasources.__all__)
