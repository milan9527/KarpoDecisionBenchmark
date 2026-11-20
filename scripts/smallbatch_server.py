"""Run the frozen batch100 implementation with a configurable private bind address."""
import argparse
import json

from batch100_server import Handler, Server, Service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--replicas", type=int, default=2)
    parser.add_argument("--max-batch", type=int, default=4)
    parser.add_argument("--token-budget", type=int, default=65536)
    parser.add_argument("--precision", choices=["fp8"], default="fp8")
    parser.add_argument("--graphs", action="store_true", default=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18450)
    args = parser.parse_args()
    service = Service(args)
    server = Server((args.host, args.port), Handler)
    server.service = service
    print(json.dumps({"event": "ready", "replicas": args.replicas}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
