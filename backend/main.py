from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx
import json
import logging
import urllib.parse
import asyncio
import re
import time
from typing import List, Dict, Any, Optional
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("olostep_api.log")  
    ]
)
logger = logging.getLogger("olostep_api")

app = FastAPI(title="Olostep API Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OLOSTEP_API_KEY = os.getenv("OLOSTEP_API_KEY")

search_cache = {}
CACHE_TTL = 3600

class SearchRequest(BaseModel):
    name: str

class SearchResponse(BaseModel):
    result: Optional[str] = None
    rawData: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

api_stats = {
    "total_requests": 0,
    "cache_hits": 0,
    "api_calls": 0,
    "errors": 0,
    "avg_response_time": 0
}

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    
    forwarded_for = request.headers.get("X-Forwarded-For")
    client_ip = forwarded_for.split(",")[0] if forwarded_for else request.client.host
    
    request_id = f"req_{int(time.time() * 1000)}"
    
    logger.info(f"[{request_id}] Request started: {request.method} {request.url.path} from {client_ip}")
    
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        
        logger.info(
            f"[{request_id}] Request completed: {request.method} {request.url.path} "
            f"- Status: {response.status_code} - Time: {process_time:.3f}s"
        )
        return response
    except Exception as e:
        process_time = time.time() - start_time
        logger.error(f"[{request_id}] Request failed: {str(e)} - Time: {process_time:.3f}s")
        return JSONResponse(
            status_code=500,
            content={"error": f"Internal server error: {str(e)}"}
        )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global exception: {exc}", exc_info=True)
    api_stats["errors"] += 1
    return JSONResponse(
        status_code=500,
        content={"error": f"Internal server error: {str(exc)}"}
    )

@app.get("/")
async def read_root():
    return {"status": "ok", "message": "FastAPI backend for Olostep API is running"}

@app.get("/stats")
async def get_stats():
    return api_stats

@app.post("/api/search", response_model=SearchResponse)
async def search_person(request: SearchRequest):
    start_time = time.time()
    request_id = f"search_{int(start_time * 1000)}"
    api_stats["total_requests"] += 1
    
    if not request.name:
        logger.warning(f"[{request_id}] Empty name provided in request")
        raise HTTPException(status_code=400, detail="Name is required")
    
    normalized_name = normalize_search_term(request.name)
    
    logger.info(f"[{request_id}] Processing search request for: '{normalized_name}' (original: '{request.name}')")
    
    cache_key = f"search:{normalized_name}"
    if cache_key in search_cache:
        cache_entry = search_cache[cache_key]
        if time.time() - cache_entry["timestamp"] < CACHE_TTL:
            api_stats["cache_hits"] += 1
            logger.info(f"[{request_id}] Cache hit for '{normalized_name}'")
            response_time = time.time() - start_time
            
            api_stats["avg_response_time"] = (
                (api_stats["avg_response_time"] * (api_stats["total_requests"] - 1) + response_time) / 
                api_stats["total_requests"]
            )
            
            return {
                "result": cache_entry["conversation_starters"],
                "rawData": cache_entry["search_results"]
            }
    
    try:
        logger.info(f"[{request_id}] Fetching data from Olostep API for '{normalized_name}'")
        api_stats["api_calls"] += 1
        
        search_results = await fetch_person_info(normalized_name, request_id)
        
        conversation_starters = process_olostep_results(normalized_name, search_results, request_id)
        
        search_cache[cache_key] = {
            "search_results": search_results,
            "conversation_starters": conversation_starters,
            "timestamp": time.time()
        }
        
        if len(search_cache) > 100:
            cleanup_cache()
        
        logger.info(f"[{request_id}] Successfully processed search for: '{normalized_name}'")
        
        response_time = time.time() - start_time
        
        api_stats["avg_response_time"] = (
            (api_stats["avg_response_time"] * (api_stats["total_requests"] - 1) + response_time) / 
            api_stats["total_requests"]
        )
        
        return {"result": conversation_starters, "rawData": search_results}
    
    except HTTPException as e:
        logger.error(f"[{request_id}] HTTP exception: {e.detail}")
        api_stats["errors"] += 1
        raise
    
    except Exception as e:
        logger.error(f"[{request_id}] Unexpected error in search_person: {e}", exc_info=True)
        api_stats["errors"] += 1
        return JSONResponse(
            status_code=500,
            content={"error": f"Error processing request: {str(e)}"}
        )

def normalize_search_term(name: str) -> str:
    """
    Normalize search terms to improve cache hits and Olostep API stability
    - Remove extra spaces
    - Convert to lowercase
    - Remove special characters that might cause issues
    """
    normalized = re.sub(r'[^\w\s]', '', name)
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip().lower()

def cleanup_cache():
    """Remove oldest entries from the cache"""
    current_time = time.time()
    sorted_cache = sorted(search_cache.items(), key=lambda x: x[1]["timestamp"])
    for key, _ in sorted_cache[:20]:
        del search_cache[key]
    logger.info(f"Cleaned up cache, removed 20 oldest entries. Cache size now: {len(search_cache)}")

async def fetch_person_info(name: str, request_id: str) -> Dict[str, Any]:
    """
    Fetch information about a person using Olostep API with improved error handling,
    timeouts, and retry logic
    """
    from httpx import AsyncClient, HTTPStatusError, TimeoutException, TransportError

    endpoint = "https://api.olostep.com/v1/scrapes"
    
    safe_name = urllib.parse.quote(name)
    
    payload = {
        "formats": ["parser_extract"],
        "parser_extract": {"parser_id": "@olostep/google-search"},
        "url_to_scrape": f"https://www.google.com/search?q={safe_name}&gl=us&hl=en",
        "wait_before_scraping": 0,
    }
    
    headers = {
        "Authorization": f"Bearer {OLOSTEP_API_KEY}",
        "Content-Type": "application/json",
    }
    
    
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            logger.info(f"[{request_id}] Attempt {attempt + 1}/{max_retries} to fetch data for '{name}'")
            
    
            async with AsyncClient(timeout=45.0) as client:
                logger.debug(f"[{request_id}] Sending request to {endpoint}")
                api_request_start = time.time()
                
                response = await client.post(endpoint, json=payload, headers=headers)
                api_request_time = time.time() - api_request_start
                
    
                logger.info(f"[{request_id}] Olostep API response status: {response.status_code}, time: {api_request_time:.3f}s")
                
                if response.status_code != 200:
                    response_text = response.text
                    logger.error(f"[{request_id}] API error response: {response_text}")
                    
    
                    if response.status_code == 429 and attempt < max_retries - 1:
                        backoff_time = 2 ** attempt
                        logger.warning(f"[{request_id}] Rate limited. Backing off for {backoff_time} seconds")
                        await asyncio.sleep(backoff_time)
                        continue
                    
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Failed to fetch from Olostep API: {response_text}"
                    )
                
                data = response.json()
                logger.info(f"[{request_id}] Received data structure: {list(data.keys())}")
                
                if "result" in data and "json_content" in data["result"]:
                    try:
                        parsed_data = json.loads(data["result"]["json_content"])
                        logger.info(f"[{request_id}] Successfully parsed JSON content")
                        
                        if not any(key in parsed_data for key in ["organic", "knowledgeGraph", "relatedSearches"]):
                            logger.warning(f"[{request_id}] Parsed JSON lacks expected keys: {list(parsed_data.keys())}")
                            
                            if attempt == max_retries - 1:
                                logger.info(f"[{request_id}] Returning empty data structure as fallback")
                                return {
                                    "organic": [],
                                    "knowledgeGraph": {},
                                    "relatedSearches": []
                                }
                        
                        return parsed_data
                        
                    except json.JSONDecodeError as e:
                        logger.error(f"[{request_id}] Failed to parse JSON content: {e}")
                        logger.error(f"[{request_id}] Raw content sample: {data['result']['json_content'][:200]}...")
                        
                        try:
                            cleaned_json = clean_json_string(data['result']['json_content'])
                            parsed_data = json.loads(cleaned_json)
                            logger.info(f"[{request_id}] Successfully parsed cleaned JSON content")
                            return parsed_data
                        except Exception as clean_error:
                            logger.error(f"[{request_id}] Failed to parse cleaned JSON: {clean_error}")
                            
                            if attempt == max_retries - 1 and "json_hosted_url" in data["result"]:
                                return await fetch_from_hosted_url(data["result"]["json_hosted_url"], request_id)
                            
                            continue
                else:
                    logger.error(f"[{request_id}] Unexpected response format. Keys: {list(data.keys())}")
                    if "result" in data:
                        logger.error(f"[{request_id}] Result keys: {list(data['result'].keys())}")
                    
                    # use json_hosted_url as fallback
                    if "result" in data and "json_hosted_url" in data["result"]:
                        return await fetch_from_hosted_url(data["result"]["json_hosted_url"], request_id)
                    
                    # return empty but valid structure
                    if attempt == max_retries - 1:
                        logger.info(f"[{request_id}] Returning empty data structure as fallback")
                        return {
                            "organic": [],
                            "knowledgeGraph": {},
                            "relatedSearches": []
                        }
                    
                    raise HTTPException(
                        status_code=500,
                        detail="Unexpected response format from Olostep API"
                    )
                
        except TimeoutException as e:
            logger.error(f"[{request_id}] Request timed out on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            
            logger.info(f"[{request_id}] Returning empty data structure after timeout")
            return {
                "organic": [],
                "knowledgeGraph": {},
                "relatedSearches": []
            }
            
        except TransportError as e:
            logger.error(f"[{request_id}] Transport error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            
            logger.info(f"[{request_id}] Returning empty data structure after transport error")
            return {
                "organic": [],
                "knowledgeGraph": {},
                "relatedSearches": []
            }
            
        except Exception as e:
            logger.error(f"[{request_id}] Unexpected error in fetch_person_info: {e}", exc_info=True)
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            
            logger.info(f"[{request_id}] Returning empty data structure after unexpected error")
            return {
                "organic": [],
                "knowledgeGraph": {},
                "relatedSearches": []
            }
    
    logger.warning(f"[{request_id}] All attempts failed, returning empty data structure")
    return {
        "organic": [],
        "knowledgeGraph": {},
        "relatedSearches": []
    }

async def fetch_from_hosted_url(json_url: str, request_id: str) -> Dict[str, Any]:
    """Fetch JSON data from Olostep's hosted URL as fallback"""
    logger.info(f"[{request_id}] Attempting to fetch JSON from hosted URL: {json_url}")
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as json_client:
            json_response = await json_client.get(json_url)
            
            if json_response.status_code == 200:
                logger.info(f"[{request_id}] Successfully fetched JSON from hosted URL")
                return json_response.json()
            else:
                logger.error(f"[{request_id}] Failed to fetch from hosted URL: {json_response.status_code}")
                
                return {
                    "organic": [],
                    "knowledgeGraph": {},
                    "relatedSearches": []
                }
    except Exception as e:
        logger.error(f"[{request_id}] Error fetching from hosted URL: {e}")
        
        return {
            "organic": [],
            "knowledgeGraph": {},
            "relatedSearches": []
        }

def clean_json_string(json_str: str) -> str:
    """Attempt to clean and fix malformed JSON"""
    cleaned = json_str.strip()
    cleaned = re.sub(r'([^\\])"([^"]*)":', r'\1"\2":', cleaned)
    cleaned = re.sub(r',\s*}', '}', cleaned)
    cleaned = re.sub(r',\s*]', ']', cleaned)
    return cleaned

def process_olostep_results(name: str, search_results: Dict[str, Any], request_id: str) -> str:
    """Process Olostep search results into conversation starters with robust error handling"""
    try:
        logger.info(f"[{request_id}] Processing search results for '{name}'")
        
        search_result_keys = list(search_results.keys())
        logger.info(f"[{request_id}] Search results keys: {search_result_keys}")
        
        organic_results = search_results.get("organic", [])
        knowledge_graph = search_results.get("knowledgeGraph", {})
        related_searches = search_results.get("relatedSearches", [])
        
        logger.info(f"[{request_id}] Found {len(organic_results)} organic results")
        logger.info(f"[{request_id}] Knowledge graph present: {bool(knowledge_graph)}")
        logger.info(f"[{request_id}] Found {len(related_searches)} related searches")
        
        conversation_starters = []
        
        if knowledge_graph and isinstance(knowledge_graph, dict):
            if "description" in knowledge_graph and knowledge_graph["description"]:
                conversation_starters.append(
                    f"🌟 I recently read that {knowledge_graph['description']} Would you like to talk more about that?"
                )
            
            if "attributes" in knowledge_graph and knowledge_graph["attributes"]:
                attributes = knowledge_graph["attributes"]
                
                if "Born" in attributes:
                    conversation_starters.append(
                        f"🏙️ I saw you were born in {attributes['Born']}. How has that shaped your perspective?"
                    )
                
                if "Education" in attributes:
                    conversation_starters.append(
                        f"🎓 Your background at {attributes['Education']} sounds fascinating. What was your most valuable takeaway from that experience?"
                    )
        
        if organic_results and len(organic_results) > 0:
            topics = []
            for result in organic_results[:3]:
                if isinstance(result, dict) and "title" in result and result["title"]:
                    title_parts = result["title"].split(" - ")
                    if title_parts and title_parts[0]:
                        topics.append(title_parts[0])
            
            for topic in topics:
                if topic and not any(topic in starter for starter in conversation_starters):
                    conversation_starters.append(
                        f"📚 I came across your work related to {topic}. What inspired you to explore that area?"
                    )
        
        for result in organic_results[:2]:
            if isinstance(result, dict) and "snippet" in result and result["snippet"]:
                snippet = result["snippet"]
                if not any(snippet[:20] in starter for starter in conversation_starters):
                    clean_snippet = snippet.replace(name, "you").rstrip("...")
                    conversation_starters.append(
                        f"💡 I read that {clean_snippet} Can you tell me more about that?"
                    )
        
        if related_searches and len(related_searches) > 0:
            interests = []
            for search in related_searches:
                if isinstance(search, dict) and "query" in search and search["query"]:
                    interest = search["query"].replace(name, "").strip()
                    if interest and "wiki" not in interest.lower() and "net worth" not in interest.lower():
                        interests.append(interest)
            
            if interests:
                conversation_starters.append(
                    f"🔍 I've been interested in {interests[0]} myself lately. Any thoughts on that area?"
                )
        
        generic_starters = [
            "💡 I've been following your journey and I'm particularly impressed by your approach to innovation. What's driving your current focus?",
            "🤝 I find your work in this field really inspiring. What's a challenge you're currently tackling?",
            "🌈 What's a project you're excited about right now that not many people know about?",
            "🧠 What's a perspective or insight you've gained recently that changed how you think about your work?",
            "⏳ Looking back on your career so far, what would you say was a pivotal moment that shaped your direction?"
        ]
        
        while len(conversation_starters) < 5:
            for starter in generic_starters:
                if starter not in conversation_starters:
                    conversation_starters.append(starter)
                    break
        
        conversation_starters = conversation_starters[:5]
        result = "\n\n".join(f"{i+1}. {starter}" for i, starter in enumerate(conversation_starters))
        
        logger.info(f"[{request_id}] Generated {len(conversation_starters)} conversation starters")
        return result
        
    except Exception as e:
        logger.error(f"[{request_id}] Error in process_olostep_results: {e}", exc_info=True)
        
        logger.info(f"[{request_id}] Returning generic conversation starters as fallback")
        generic_result = "\n\n".join([
            "1. 💡 I've been following your journey and I'm particularly impressed by your approach to innovation. What's driving your current focus?",
            "2. 🤝 I find your work in this field really inspiring. What's a challenge you're currently tackling?",
            "3. 🌈 What's a project you're excited about right now that not many people know about?",
            "4. 🧠 What's a perspective or insight you've gained recently that changed how you think about your work?",
            "5. ⏳ Looking back on your career so far, what would you say was a pivotal moment that shaped your direction?"
        ])
        return generic_result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)