import os
import random
import asyncio
import re
import numpy as np
import faiss
import streamlit as st
from dotenv import load_dotenv
from groq import Groq, RateLimitError
from sentence_transformers import SentenceTransformer
from supabase import create_client, Client
from huggingface_hub import InferenceClient
import torch
import pandas as pd
import json
import uuid

load_dotenv(dotenv_path=".env")

torch.classes.__path__ = []

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(url, key)

api_keys = [os.getenv("GROQ_API_KEY")]
api_index = 0
client = Groq(api_key=api_keys[api_index])
model = "llama-3.3-70b-versatile"

protectai_client = InferenceClient(
    provider="hf-inference",
    api_key=os.getenv("PROTECTAI_API_KEY"),
)

session_history = []
cache = {}


def load_from_bucket(file_name):
    with open(f"{file_name}", "wb+") as f:
        response = supabase.storage.from_("rag").download(f"{file_name}")
        f.write(response)
    return file_name


def load_embeddings(file):
    data_src_index = faiss.read_index(load_from_bucket(file))
    return data_src_index


async def save_session_to_supabase(session_id, messages):
    data = {"session_id": str(session_id), "messages": json.dumps(messages, indent=4)}

    await asyncio.to_thread(
        lambda: supabase.table("session_history").upsert(data).execute()
    )


def extract_filtered_json_data(data, matched_keys):
    filtered_data = data.iloc[matched_keys, :]

    grouped_json = (
        filtered_data.groupby(["topic", "lesson_title"], group_keys=False)
        .apply(
            lambda x: [
                list(x["course_title"].unique()),
                list(x["language"].unique()),
                x[["problem_title", "difficulty", "type"]]
                .drop_duplicates()
                .to_dict(orient="records"),
            ],
            include_groups=False,
        )
        .reset_index()
    )

    grouped_json.columns = ["topic", "lesson_title", "data"]

    final_output = [
        {
            "supplementary_courses": row["data"][0],
            "topic": row["topic"],
            "lesson_title": row["lesson_title"],
            "practice_problems": row["data"][2],
            "languages": row["data"][1],
        }
        for _, row in grouped_json.iterrows()
    ]
    return final_output

def extract_from_np(data_src, indices):
    related_data = []
    for index in indices:
        data_list = data_src["chunk"].tolist() 
        related_data.append(data_list[index])

    return related_data

def find_relevant_src(index, data_src, type, user_query):
    embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    query_embeddings = embedding_model.encode([user_query])

    k = 4
    distances, indices = index.search(query_embeddings, k)
    good_results = pd.DataFrame(
        [(idx, dist) for idx, dist in zip(indices[0], distances[0]) if dist < 1]
    )
    # display(good_results)

    # for _, row in good_results.iterrows():
    #     display(df_data_src.iloc[row[0].astype(int)])

    related_data = []
    if len(good_results) > 0:
        if type == "json":
            extracted_data = extract_filtered_json_data(data_src, good_results[0].tolist())
        else:
            extracted_data = extract_from_np(data_src, good_results[0].tolist())
        related_data.extend(extracted_data)

    return related_data


async def call_api_with_retry(messages, max_retries=5):
    global api_index, client
    retries = 0
    messages = messages[-7:]
    while retries < max_retries:
        try:
            output = client.chat.completions.create(
                messages=messages, model=model, max_tokens=1024, stream=True
            )
            response = ""
            for chunk in output:
                content = chunk.choices[0].delta.content
                if content:
                    response += content
            return response
        except RateLimitError as e:
            error_msg = str(e)
            api_index = (api_index + 1) % len(api_keys)
            client = Groq(api_key=api_keys[api_index])
            wait_time = (
                float(re.search(r"Please try again in ([\d.]+)s", error_msg).group(1))
                if re.search(r"Please try again in ([\d.]+)s", error_msg)
                else (2**retries) + random.uniform(0, 1)
            )
            await asyncio.sleep(wait_time)
            retries += 1
    return "I'm currently experiencing high traffic. Please try again in a moment."


def display_text(response):
    segments = re.split(r"(```.*?```)", response, flags=re.DOTALL)
    for segment in segments:
        if segment.startswith("```") and segment.endswith("```"):
            st.code(segment.strip("`\n"))
        else:
            st.markdown(segment)


async def generate_response():
    #
    # TODO: Add caching to minimize token limit error i guess
    # cache_key = tuple(msg["content"] for msg in session_history if msg["role"] == "user")
    # if cache_key in cache:
    #     response = cache[cache_key]
    # else:
    response = await call_api_with_retry(st.session_state.messages)
    # if response:
    #     cache[cache_key] = response

    result = {"role": "assistant", "content": response}
    if len(st.session_state.messages) > 1:
        with st.chat_message("assistant"):
            display_text(response)
    session_history.append(result)
    st.session_state.messages.append(result)
    await save_session_to_supabase(
        st.session_state.session_id, st.session_state.messages
    )


def is_injection(text, threshold=0.95):
    classification_result = protectai_client.text_classification(
        text=text,
        model="protectai/deberta-v3-base-prompt-injection-v2",
    )
    print("Classification result:", classification_result)
    for result in classification_result:
        if result.label.upper() == "INJECTION" and result.score >= threshold:
            return True
    return False


st.title("Learning Assistant (with CodeChum)")

if "session_id" not in st.session_state:
    session_id = uuid.uuid4()
    st.session_state.session_id = session_id
    print("Session ID:", st.session_state.session_id)

if "messages" not in st.session_state:
    st.session_state.messages = []
    system_prompt = {"role": "system", "content": "Greet the user"}
    st.session_state.messages.append(system_prompt)
    session_history.append(system_prompt)
    asyncio.run(generate_response())

print(st.session_state.messages)

if "data_index" not in st.session_state:
    print("Initializing data index")
    st.session_state.data_index = load_embeddings("course_embeddings_v3.index")
    st.session_state.bst_index = load_embeddings("bst_embeddings.index")

if "data_src" not in st.session_state:
    print("Initializing data source")
    st.session_state.data_src = pd.read_csv(load_from_bucket("codechum_src.csv"))
    st.session_state.bst_src = pd.read_csv(load_from_bucket("bst_src.csv"))

data_index = st.session_state.data_index
data_src = st.session_state.data_src

for message in st.session_state.messages:
    if message["role"] != "system" and message["role"] != "tool":
        with st.chat_message(message["role"]):
            display_text(message["content"])

if prompt := st.chat_input("Ask something"):
    with st.chat_message("user"):
        display_text(prompt)

    if is_injection(prompt):
        prompt = f"{os.getenv("PROMPT_INJECTION_FLAG_PROMPT")} {prompt}"

    relevant_data = find_relevant_src(data_index, data_src, "json", prompt)
    bst_relevant_data = find_relevant_src(st.session_state.bst_index, st.session_state.bst_src, "np", prompt)
    
    user_prompt = {"role": "user", "content": prompt}
    session_history.append(user_prompt)
    st.session_state.messages.append(user_prompt)
    st.session_state.messages.append(
        {"role": "system", "content": os.getenv("TEST_MODE_GUIDELINES")}
    )
    if relevant_data:
        relevant_data_str = json.dumps(
            relevant_data, indent=4
        )  # Convert JSON to string
        st.session_state.messages.append(
            {
                "role": "system",
                "content": "Include this data (have it in a list format) from Codechum for suggestions:\n"
                + relevant_data_str
            }
        )
    if bst_relevant_data:
        relevant_data_str = json.dumps(
            bst_relevant_data, indent=4
        )  # Convert JSON to string
        st.session_state.messages.append(
            {
                "role": "system",
                "content": "Remember that this data is separate from Codechum. Include this data:\n"
                + relevant_data_str
            }
        )
    print(st.session_state.messages)
    # print(os.getenv('TEST_MODE_GUIDELINES'))
    asyncio.run(generate_response())
    for msg in st.session_state.messages:
        if msg["role"] == "system":
            st.session_state.messages.remove(msg)
