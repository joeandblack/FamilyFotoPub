"""Shared authorization and identifier validation for authenticated API routers."""
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request, Response
MAX_ID = 9007199254740991
Identifier = Annotated[int, Path(gt=0, le=MAX_ID)]

def reader(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    user = getattr(request.state, 'user', None)
    if not user or not getattr(request.state, 'authenticated', False):
        raise HTTPException(401, 'Login required')
    if user.get('role') not in ('admin', 'regular', 'guest'):
        raise HTTPException(403, 'Face results are not available to this account')
    return user

def administrator(user=Depends(reader)):
    if user['role'] != 'admin':
        raise HTTPException(403, 'Only an administrator can operate face detection')
    return user
