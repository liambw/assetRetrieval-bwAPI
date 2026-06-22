import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bw-api"))
