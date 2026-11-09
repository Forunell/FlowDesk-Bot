import argparse


def main():
    parser = argparse.ArgumentParser(description="FlowDesk local demo and optional Telegram bot")
    parser.add_argument("mode", choices=["demo", "telegram"], nargs="?", default="demo")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.mode == "telegram":
        from .telegram import run
        run()
    else:
        import uvicorn

        from .web import create_app
        print("FlowDesk: http://127.0.0.1:%s/demo" % args.port)
        print("Admin password: open the local .env file (FLOWDESK_ADMIN_PASSWORD).")
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
