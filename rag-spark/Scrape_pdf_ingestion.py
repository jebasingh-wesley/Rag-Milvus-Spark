import os
import re
import requests
from bs4 import BeautifulSoup
from pyspark.sql import SparkSession
from pyspark.sql.functions import udf, explode
from pyspark.sql.types import StringType, ArrayType, FloatType
from sentence_transformers import SentenceTransformer
from pymilvus import connections, Collection
from dotenv import load_dotenv

load_dotenv()

os.environ['PYARROW_IGNORE_TIMEZONE'] = '1'
os.environ['NUMEXPR_MAX_THREADS'] = '2'
os.environ['NUMEXPR_NUM_THREADS'] = '2'
os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'

# Initialize Spark
spark = SparkSession.builder.getOrCreate()
sc = spark.sparkContext
sc.setLogLevel('ERROR')
spark.conf.set("spark.sql.shuffle.partitions", 2)

CHUNK_SIZE = 1600
CHUNK_OVERLAP = 50

# Define the function to create embeddings
def create_embedding(text):
    transformer = SentenceTransformer(os.getenv('EMBEDDING_MODEL'))
    embeddings = transformer.encode(text, convert_to_tensor=True)
    return embeddings.numpy().tolist()

# Function to scrape text from a webpage
def scrape_text_from_url(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'html.parser')
    paragraphs = soup.find_all('p')
    text = ' '.join([para.get_text() for para in paragraphs])
    return text

# Define the function to split text into chunks
def extract_text_chunks(symbol, text):
    metadata = "Document contains context of " + symbol \
        + " and is relevant to the scraped content from website.\n"
    chunks = []
    for i in range(0, len(text), CHUNK_SIZE):
        if i > CHUNK_OVERLAP:
            chunks.append(metadata + text[i - CHUNK_OVERLAP: i + CHUNK_SIZE])
        else:
            chunks.append(metadata + text[i: i + CHUNK_SIZE])
    return chunks

# Function to extract stock symbol from a URL or fallback identifier
def get_stock_symbol(url):
    match = re.search(r'NASDAQ_([A-Z]{1,5})', url)
    if match:
        return match.group(1)
    return "NA"

# Register UDFs
scrape_text_udf = udf(scrape_text_from_url, StringType())
extract_text_chunks_udf = udf(extract_text_chunks, ArrayType(StringType()))
create_embedding_udf = udf(create_embedding, ArrayType(FloatType()))
get_stock_symbol_udf = udf(get_stock_symbol, StringType())

spark.udf.register("scrape_text", scrape_text_udf)
spark.udf.register("extract_text_chunks", extract_text_chunks_udf)
spark.udf.register("create_embeddings", create_embedding_udf)
spark.udf.register("get_stock_symbol", get_stock_symbol_udf)

def get_embedded_chunks(urls):
    # Create a DataFrame with the URLs
    url_df = spark.createDataFrame(urls, "string").toDF("url")
    url_df = url_df.select('url', get_stock_symbol_udf('url').alias('stock_symbol'))

    # Scrape text from each URL
    scraped_data = url_df.withColumn("text", scrape_text_udf("url"))

    # Split text into chunks
    chunked_text_data = scraped_data.withColumn("relevant_text", 
                                                extract_text_chunks_udf("stock_symbol", "text"))
    
    # Break text into individual rows for each chunk
    chunked_text_data = chunked_text_data.select('stock_symbol', 'url', 
                                                 explode(chunked_text_data.relevant_text).alias('chunked_text'))

    # Create embeddings for each text chunk
    chunked_text_data = chunked_text_data.withColumn("embedded_vectors", 
                                                     create_embedding_udf("chunked_text"))
    return chunked_text_data

def ingest_data():
    print("Data ingestion started...")
    urls = [
        "https://en.wikipedia.org/wiki/Apple_Inc.",
        "https://en.wikipedia.org/wiki/Google",
        # Add more URLs here as needed
    ]
    
    # Get embedded chunks from scraped data
    chunked_data = get_embedded_chunks(urls)

    # Connect to Milvus Database
    connections.connect(host=os.getenv('MILVUS_HOST'), port=os.getenv('MILVUS_PORT'), secure=False)
    
    # Insert data into Milvus
    collection_name = os.getenv('MILVUS_COLLECTION_NAME')
    collection = Collection(collection_name)
    collection.insert(chunked_data.toPandas())
    collection.flush()
    print("Data ingestion completed...")

if __name__ == "__main__":
    ingest_data()
