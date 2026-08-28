from pathlib import Path

PROJECT_ROOT = Path.cwd()
MAINTAINED_SOURCE_ROOTS = (
    PROJECT_ROOT / "app",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "src" / "clients" / "web" / "src",
    PROJECT_ROOT / "src" / "workspace-services",
    PROJECT_ROOT / "tests",
)
IGNORED_DIRECTORY_NAMES = {"__pycache__", "node_modules"}


def _source_files() -> list[Path]:
    files: list[Path] = []
    audit_file = Path(__file__).resolve()
    for root in MAINTAINED_SOURCE_ROOTS:
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.resolve() != audit_file
            and not any(part in IGNORED_DIRECTORY_NAMES for part in path.parts)
        )
    return files


def _source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _source_files())


def test_proto_is_the_only_public_protocol_source() -> None:
    assert (PROJECT_ROOT / "proto" / "boxteam" / "common" / "v1").is_dir()
    assert (PROJECT_ROOT / "proto" / "boxteam" / "gateway" / "v1").is_dir()
    assert (PROJECT_ROOT / "proto" / "boxteam" / "workspace" / "v2").is_dir()
    assert (PROJECT_ROOT / "proto" / "boxteam" / "terminal" / "v1").is_dir()
    assert (PROJECT_ROOT / "proto" / "boxteam" / "browser" / "v1").is_dir()
    assert (PROJECT_ROOT / "proto" / "boxteam" / "workspace" / "v2" / "public.proto").is_file()
    assert (PROJECT_ROOT / "proto" / "boxteam" / "gateway" / "v1" / "public.proto").is_file()
    assert (PROJECT_ROOT / "scripts" / "generate_protocol.mjs").is_file()
    assert not (PROJECT_ROOT / "scripts" / "generate_public_types.mjs").exists()


def test_internal_pydantic_models_are_not_named_or_exported_as_public_protocol() -> None:
    assert (PROJECT_ROOT / "app" / "schemas" / "internal_v2").is_dir()
    assert not (PROJECT_ROOT / "app" / "schemas" / "public_v2").exists()

    source_text = _source_text()
    forbidden_markers = (
        "public_v2",
        "pydantic2ts",
        "pydantic-to-typescript",
        "generate_public_types",
        "gen:public-types",
    )
    for marker in forbidden_markers:
        assert marker not in source_text, f"维护中的源码仍包含废弃协议来源标记: {marker}"


def test_web_types_do_not_retain_the_old_generated_dto_tree() -> None:
    generated_dto_root = PROJECT_ROOT / "src" / "clients" / "web" / "src" / "types" / "gen"
    assert not generated_dto_root.exists()
    assert (PROJECT_ROOT / "src" / "clients" / "web" / "src" / "types" / "openapi" / "index.json").is_file()

    backend_types = (PROJECT_ROOT / "src" / "clients" / "web" / "src" / "types" / "backend.ts").read_text(
        encoding="utf-8"
    )
    protocol_types = (PROJECT_ROOT / "src" / "clients" / "web" / "src" / "types" / "protocol.ts").read_text(
        encoding="utf-8"
    )
    assert 'from "./protocol"' in backend_types
    assert "protocol_generated" in protocol_types
