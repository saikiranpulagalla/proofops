class PlannerOutputError(ValueError):
    def __init__(self, message: str, *, raw_output: str = ""):
        self.raw_output = raw_output
        super().__init__(message)
