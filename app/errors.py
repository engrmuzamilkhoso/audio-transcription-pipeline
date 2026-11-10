class AppError(Exception):
    """An expected error we show to the client, with a stable code and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status