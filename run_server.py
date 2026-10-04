"""启动自适应中文测评 HTTP 服务。

用法：
    python run_server.py [--catalog examples/catalog_seed.json] \\
        [--data ./.data] [--host 127.0.0.1] [--port 8080]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from adaptive_exam.api import serve
from adaptive_exam.catalog import load_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="自适应中文测评服务")
    parser.add_argument("--catalog", default="examples/catalog_seed.json")
    parser.add_argument("--data", default="./.data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    data_dir = Path(args.data)
    catalog = load_catalog(args.catalog)
    server = serve(
        catalog=catalog,
        session_dir=str(data_dir / "sessions"),
        exposure_path=str(data_dir / "exposures.json"),
        host=args.host,
        port=args.port,
    )
    print(
        f"服务已启动: http://{args.host}:{args.port}  "
        f"(图谱 {sorted(catalog.graphs)}, 题库 {sorted(catalog.banks)}, "
        f"规则 {sorted(catalog.rules)})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务……")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
