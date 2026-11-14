DESCRIPTION='Service-day timetable routing with auditable transfer itineraries'
import argparse, json, sys
from pathlib import Path
from .io import publish
from .pipeline import run

def main():
    p=argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--pareto',action='store_true',
                   help='emit all non-dominated (arrival, transfers) journeys instead of the earliest-arrival one')
    a=p.parse_args()
    try:
        if a.source.resolve()==a.output.resolve(): raise ValueError('source and output must differ')
        result=run(a.source,pareto_mode=a.pareto);publish(a.output,result)
    except (ValueError,OSError,KeyError,TypeError,OverflowError,RecursionError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False));return 0

if __name__=='__main__': raise SystemExit(main())
