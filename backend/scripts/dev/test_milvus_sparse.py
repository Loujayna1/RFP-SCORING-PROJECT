import sys
from pymilvus import connections, utility, Collection, CollectionSchema, FieldSchema, DataType

def run_test():
    print("Connecting to Milvus...")
    connections.connect("default", host="milvus", port="19530")
    
    col_name = "test_hybrid_collection"
    if utility.has_collection(col_name):
        print(f"Dropping existing collection {col_name}...")
        utility.drop_collection(col_name)
        
    print("Defining schema...")
    fields = [
        FieldSchema(name="pk", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=4),
        FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR),
    ]
    schema = CollectionSchema(fields, description="test hybrid search schema")
    
    print("Creating collection...")
    col = Collection(col_name, schema)
    
    print("Creating indexes...")
    dense_params = {
        "metric_type": "COSINE",
        "index_type": "IVF_FLAT",
        "params": {"nlist": 128}
    }
    sparse_params = {
        "metric_type": "IP",
        "index_type": "SPARSE_INVERTED_INDEX",
        "params": {"drop_ratio_build": 0.2}
    }
    col.create_index("embedding", dense_params)
    col.create_index("sparse_vector", sparse_params)
    
    print("Loading collection...")
    col.load()
    
    print("Inserting data...")
    data = [
        ["id1", "id2"],
        [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]],
        [{0: 0.5, 1: 0.6}, {2: 0.8}]
    ]
    col.insert(data)
    col.flush()
    print("Entity count:", col.num_entities)
    
    print("Searching...")
    # Test hybrid search using AnnSearchRequest + HybridSearch
    from pymilvus import AnnSearchRequest, RRFRanker
    
    req_dense = AnnSearchRequest(
        data=[[0.1, 0.2, 0.3, 0.4]],
        anns_field="embedding",
        param=dense_params,
        limit=2
    )
    req_sparse = AnnSearchRequest(
        data=[{0: 0.5}],
        anns_field="sparse_vector",
        param=sparse_params,
        limit=2
    )
    
    print("Executing hybrid search...")
    res = col.hybrid_search(
        reqs=[req_dense, req_sparse],
        rerank=RRFRanker(),
        limit=2
    )
    
    for hit in res[0]:
        print("Hit:", hit.id, "score:", hit.score)
        
    print("Dropping test collection...")
    utility.drop_collection(col_name)
    print("Test finished successfully!")

if __name__ == "__main__":
    try:
        run_test()
    except Exception as e:
        import traceback
        traceback.print_exc()
