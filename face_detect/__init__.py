"""FamilyFoto native face processing. ML dependencies load only on demand."""

def create_service(config, **kwargs):
    from .service import create_service as create
    return create(config, **kwargs)
