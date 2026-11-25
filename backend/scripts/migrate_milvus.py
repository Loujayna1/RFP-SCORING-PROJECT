import asyncio
import logging
from pymilvus import connections, utility
from app.services.vector_store import get_vector_store, _collection_schema

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def migrate_milvus_collections():
    """
    Drop all existing collections that do not have the sparse_vector field,
    and recreate them with the new schema.
    """
    vs = get_vector_store()
    await vs.connect()

    logger.info("Listing all collections...")
    collections = utility.list_collections()
    logger.info(f"Found {len(collections)} collections: {collections}")

    for name in collections:
        logger.info(f"Checking collection: {name}")
        # _get_or_create_collection automatically drops and recreates
        # if the schema is old. So calling it for each collection is enough.
        vs._get_or_create_collection(name)
        
    logger.info("Migration complete.")

if __name__ == "__main__":
    asyncio.run(migrate_milvus_collections())
