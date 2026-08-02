from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_compose_declares_seed_gate_health_and_dedicated_demo_volume() -> None:
    compose = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "demo-data-init:" in compose
    assert "env_file:\n      - ./agent-runtime/.env" in compose
    assert "condition: service_completed_successfully" in compose
    assert "healthcheck:" in compose
    assert "market-desk-demo-data:/data" in compose
    assert "CRE_DATA_VOLUME" not in compose


def test_container_copies_the_packaged_demo_initializer() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY demo ./demo" in dockerfile
    assert "cre --data-dir" not in dockerfile.split("CMD", maxsplit=1)[-1]


def test_server_refuses_a_demo_marker_outside_demo_mode() -> None:
    server = (_ROOT / "agent-runtime" / "src" / "server.ts").read_text(
        encoding="utf-8"
    )

    assert "MARKET_DESK_MODE" in server
    assert ".nan-fung-demo-data.v1.json" in server
    assert "allowed_refresh_profiles: mode === \"demo\" ? []" in server
