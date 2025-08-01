# mock agent for testing github integration

import json
from agentic.common import Agent, AgentRunner 
from agentic.models import GPT_4O_MINI # model

from dotenv import load_dotenv
import openai
import requests
import os


load_dotenv()  # This loads variables from .env into os.environ
openai.api_key = os.getenv("OPENAI_API_KEY") # api key
pr_id = os.getenv("PR_ID")
repo_owner = os.getenv("REPO_OWNER")
repo_name = os.getenv("REPO_NAME")
gh_api = os.getenv("GITHUB_API_KEY")

# Define the agent
agent = Agent(
    name="Mock PR Summary Agent",

    # Agent instructions
    instructions="""
    You are a helpful mock PR sumary agent to test github integration.
    Create a short, helpful example of a PR summary.
    """,
    
    model=GPT_4O_MINI, # model

)

# basic main function that allows us to run our agent locally in terminal
if __name__ == "__main__":
    output = agent.grab_final_result(
        "You were triggered by a PR. Follow your instructions."
    )

    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{pr_id}/comments"

    headers = {
        "Authorization": f"token {gh_api}",
    }

    data = {
        "body": output
    }

    print(requests.post(url=url,headers=headers,data=json.dumps(data)))