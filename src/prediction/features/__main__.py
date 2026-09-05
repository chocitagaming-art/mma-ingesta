import argparse

from .output import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Genera el dataset de entrenamiento.")
    parser.add_argument(
        "--write-table",
        action="store_true",
        help=(
            "Ademas del CSV, reescribe la tabla fight_prediction_training_data en la "
            "base. Hace DROP TABLE + CREATE + INSERT sobre PRODUCCION: usar a sabiendas."
        ),
    )
    args = parser.parse_args()
    main(write_table=args.write_table)
