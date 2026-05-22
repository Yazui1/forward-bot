from forward_bot.main import run

if __name__ == "__main__":
    import asyncio
    from forward_bot.main import parse_args

    args = parse_args()
    asyncio.run(run(args.config))
