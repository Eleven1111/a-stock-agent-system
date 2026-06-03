import sys, os

BASE = os.path.dirname(__file__)
PROJ = os.path.abspath(os.path.join(BASE, '..'))
sys.path.insert(0, os.path.join(PROJ, 'skills', 'stock-triage', 'scripts'))
sys.path.insert(0, os.path.join(PROJ, 'skills', 'common'))
sys.path.insert(0, PROJ)
