from fastapi import APIRouter, Depends
from auth import get_current_user
from news import score_sentiment

router = APIRouter(prefix="/sentiment", tags=["sentiment"])


@router.get("/{symbol}")
async def sentiment_for(symbol: str, user=Depends(get_current_user)):
    return await score_sentiment(symbol.upper())
