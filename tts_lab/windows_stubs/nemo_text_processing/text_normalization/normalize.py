class Normalizer:
    """Pass-through shim; SILMA is invoked with number normalization disabled."""

    def __init__(self, *args, **kwargs):
        pass

    def normalize(self, text, **kwargs):
        return text
