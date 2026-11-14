from .io import load
from .route import route
from .pareto import pareto

def run(path, pareto_mode=False):
    data=load(path)
    return pareto(data) if pareto_mode else route(data)
