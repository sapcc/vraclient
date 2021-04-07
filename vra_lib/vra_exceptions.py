class HttpUnsuccessfulException(Exception):
    """vRA http exception handler"""
    def __init__(self, message):
        self.message = message
        super(HttpUnsuccessfulException, self).__init__(self.message)
