"""Create/refresh the LangSmith datasets used to evaluate every agent.

    python -m evals.upload_dataset --dry-run    # show what would be uploaded
    python -m evals.upload_dataset              # upload new cases to the LangSmith project in .env

This sends saved research, qualification reports and generated text (restaurant names, gaps, evidence) to LangSmith.
Cases already uploaded are skipped, so it is safe to re-run. (`python -m evals.run_all --upload-only` does the same.)
"""

import argparse

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the cases without uploading")
    args = parser.parse_args()

    load_dotenv()
    from evals.run_all import registry

    agents = registry()
    if args.dry_run:
        for name, module in agents.items():
            examples = module.build_examples()
            print(f"{name}: {len(examples)} case(s) for dataset '{module.DATASET}'")
            for example in examples[:6]:
                print(f"   - {example['key']} {example.get('metadata', {}).get('restaurant', '')}")
        return 0

    from langsmith import Client

    from evals.common import upsert_examples

    client = Client()
    for name, module in agents.items():
        new, existing = upsert_examples(client, module.DATASET, module.DESCRIPTION, module.build_examples())
        print(f"{name}: '{module.DATASET}' uploaded {new} new case(s), {existing} already present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
