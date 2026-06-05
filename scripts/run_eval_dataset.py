#!/usr/bin/env python3
"""
Запуск evaluation dataset.

Использование:
  python scripts/run_eval_dataset.py
  python scripts/run_eval_dataset.py --company-id <uuid>
  python scripts/run_eval_dataset.py --verbose
"""
import asyncio
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main():
    parser = argparse.ArgumentParser(description="Run agent evaluation dataset")
    parser.add_argument("--company-id", type=str, help="Filter by company UUID")
    parser.add_argument("--verbose", action="store_true", help="Show per-tender details")
    args = parser.parse_args()

    from app.core.database import AsyncSessionLocal
    from app.eval_dataset.service import run_evaluation
    from uuid import UUID

    company_id = UUID(args.company_id) if args.company_id else None

    async with AsyncSessionLocal() as db:
        print("Running evaluation dataset...")
        results = await run_evaluation(db, company_id=company_id)

    print(f"\n{'='*50}")
    print(f"EVALUATION RESULTS")
    print(f"{'='*50}")
    print(f"Total:       {results['total']}")
    print(f"Correct:     {results['correct']}")
    print(f"Accuracy:    {results['accuracy_pct']}%")
    print(f"")
    print(f"FALSE GO:    {results['false_go']}  (агент: участвовать, реально: плохой)")
    print(f"FALSE NO_GO: {results['false_no_go']} (агент: не участвовать, реально: хороший)")
    print(f"Errors:      {results['errors']}")

    if args.verbose and results['details']:
        print(f"\n{'='*50}")
        print(f"PER-TENDER DETAILS")
        print(f"{'='*50}")
        for d in results['details']:
            if 'error' in d:
                print(f"  ❌ {d['tender_id']}: ERROR — {d['error']}")
            elif d['correct']:
                print(f"  ✅ {d['tender_id']}: {d['expected']} == {d['agent']}")
            else:
                marker = "🔴" if d['agent'] == 'go' and d['expected'] == 'no_go' else "🟡"
                print(f"  {marker} {d['tender_id']}: expected={d['expected']}, agent={d['agent']}")

    # Exit code: 0 если accuracy >= 70%, 1 иначе
    sys.exit(0 if results['accuracy_pct'] >= 70 else 1)

if __name__ == "__main__":
    asyncio.run(main())
