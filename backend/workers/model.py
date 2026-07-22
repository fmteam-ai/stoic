"""Model worker — scheduled offline retraining + model registry audit."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import _model_maintenance_loop
    main("model", [_model_maintenance_loop])
