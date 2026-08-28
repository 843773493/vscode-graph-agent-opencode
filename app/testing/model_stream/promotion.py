from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .assets import load_cassette
from .errors import ModelStreamAssetError


def promote_recorded_cassette(
    source_path: Path | str,
    *,
    fixture_root: Path | str,
    destination: Path | str,
    force: bool = False,
) -> Path:
    """将已校验的 recorded artifact 显式晋升到长期 recorded fixture。"""

    source = Path(source_path).expanduser().resolve()
    cassette = load_cassette(source)
    if cassette.metadata.get("source") != "recorded":
        raise ModelStreamAssetError(
            f"只有 source=recorded 的 cassette 可以晋升: {source}"
        )

    root = Path(fixture_root).expanduser().resolve()
    relative_destination = Path(destination)
    if relative_destination.is_absolute():
        raise ModelStreamAssetError("fixture 晋升 destination 必须是相对 fixture_root 的路径")
    target = (root / relative_destination).resolve()
    if not target.is_relative_to(root):
        raise ModelStreamAssetError(
            f"fixture 晋升 destination 不得跳出 fixture_root: {destination!s}"
        )
    if relative_destination.parts[:1] != ("recorded",):
        raise ModelStreamAssetError(
            "fixture 晋升 destination 必须位于 recorded/ 目录下"
        )
    if target.suffix != ".json":
        raise ModelStreamAssetError("fixture 晋升 destination 必须使用 .json 后缀")
    if target.exists() and not force:
        raise FileExistsError(
            f"fixture 晋升目标已存在；如需覆盖请显式传入 force=True: {target}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(cassette.raw, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as temporary:
        temporary.write(encoded)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="显式晋升已校验的 model stream recorded cassette"
    )
    parser.add_argument("source", type=Path, help="artifacts 中的完整 recorded cassette")
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在目标")
    args = parser.parse_args()
    target = promote_recorded_cassette(
        args.source,
        fixture_root=args.fixture_root,
        destination=args.destination,
        force=args.force,
    )
    print(target)


if __name__ == "__main__":
    main()
