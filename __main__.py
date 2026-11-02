"""python -m datamate.ingest <roots...> [--out=build] [--llm] [--reclassify]"""
import sys

from .pipeline import main

main(sys.argv[1:])
