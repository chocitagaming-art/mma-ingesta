from .output import main, parse_args

if __name__ == "__main__":
    args = parse_args()
    main(write_table=args.write_table, espn_snapshot=args.espn_snapshot)
