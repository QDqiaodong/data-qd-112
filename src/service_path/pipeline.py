from .io import load
from .route import route

def run(path):
    return route(load(path))
