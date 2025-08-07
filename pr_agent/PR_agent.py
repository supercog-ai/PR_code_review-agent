import os
import requests
from typing import List, Dict
from pydantic import Field, BaseModel
from dotenv import load_dotenv
from agentic.common import Agent
from agentic.models import GPT_4O_MINI
from git_grep_agent import GitGrepAgent
from summary_agent import SummaryAgent
from pydantic import BaseModel

load_dotenv()

class Searches(BaseModel):
    searches: List[str] = Field(
        description="Search queries."
    )

# list workaround for model responses
class ListType(BaseModel):
    items: List[str] = Field(
        description="List of items."
    )

class PRReviewAgent():

    def __init__(self):
        self.git_grep_agent = GitGrepAgent()

        self.queryAgent = Agent(
            name="Code Query Agent",
            instructions="""You are a static code analysis agent. You will be given a patch file (diff) showing code changes made to a source codebase.

Your goal is to extract a list of search terms that can be used with git grep to locate code relevant to the changes but defined elsewhere in the codebase.

Specifically, extract symbols that meet all of the following criteria:

Used in the patch (referenced or invoked).

Not defined in the patch (not declared, assigned, implemented, or modified as a definition).

Not part of a known module or standard library (i.e., imported or obvious system-provided identifiers).

Likely defined elsewhere in the same project — e.g., internal utility functions, constants, types, classes, etc.

Additional requirements:

Do not include anything that is added or changed in the patch’s + lines if it defines a symbol.

Do not include anything from lines that import, include, or reference known external modules (e.g. import re, from datetime import ..., #include <stdlib.h>, etc.).

Skip overly broad or generic patterns. Only include identifiers that are likely unique enough to help pinpoint related code.

Be strict: if a symbol was defined in the patch, do not include it.""",
            model=GPT_4O_MINI,
            result_model=Searches,
        )

        self.summaryAgent = SummaryAgent()

    def prepare_summary(self, patch_content: str, filtered_results: Dict[str, str]) -> str:
        """Prepare for summary agent"""
        formatted_str = ""
        formatted_str += f"<Patch file>\n"
        formatted_str += f"{patch_content}\n"
        formatted_str += f"</Patch File>\n\n"
        
        for file_path, content in filtered_results.items():
            formatted_str += f"<{file_path}>\n"
            formatted_str += f"{content}\n"
            formatted_str += f"</{file_path}>\n\n"

        return formatted_str

    def post_to_github(self, summary: str) -> str:
        """Post summary as a GitHub comment"""
        repo_owner = os.getenv("REPO_OWNER")
        repo_name = os.getenv("REPO_NAME")
        pr_id = os.getenv("PR_ID")
        gh_token = os.getenv("GITHUB_API_KEY")
        
        if not all([repo_owner, repo_name, pr_id, gh_token]):
            raise ValueError("Missing required GitHub configuration")
            
        url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{pr_id}/comments"
        headers = {
            "Authorization": f"token {gh_token}",
        }
        data = {"body": summary}
        
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json().get("html_url")


    def generate(self, patch_content: str) -> str:
        # Generate search queries
        queries = self.queryAgent << patch_content

        # Git-Grep queries
        all_results = {}
        for query in queries.searches[:10]:
            searchResponse = self.git_grep_agent.get_search(query) # returns a dictionary of file paths and their contents

            # concatenate to all_results
            for file_path, content in searchResponse.items():
                if file_path not in all_results:
                    all_results[file_path] = content

        # vet for unneeded files
        vetting = Agent(
            name="Vetting Agent",
            instructions="""
            You are an AI agent that identifies source files most relevant to a given code patch.

            You are provided with:

            A patch file (unified diff format), which modifies certain functions, methods, variables, or logic.

            A list of file paths obtained via git grep, which may or may not be relevant.

            Your task is to analyze the patch semantically and determine which files in the list are most directly related to the code being changed, based on actual dependencies, references, or interactions — not just keyword overlap.

            What You Should Do:

            Extract the following from the patch:

            Modified function names, variables, class names, or symbols

            Any new or altered function calls (e.g. sanitize_input())

            Any imported modules or utilities that may have changed

            Contextual information (e.g. updated behavior, logic, or error handling)

            For each file in the provided list:

            Evaluate whether it defines, imports, calls, or depends on any of the symbols, functions, or classes involved in the patch.

            Prefer files that have functional or logical connections (e.g. where the modified function is called, or where a helper function is defined).

            De-prioritize files with only surface-level textual matches or unrelated content.

            Ignore files that share keywords but have no semantic connection to the changed code.

            Give preference to files that define or use identifiers appearing in the patch.

            If necessary, infer relevance based on common architectural patterns (e.g. services call validators, tests cover services, etc.).
            """,
            model=GPT_4O_MINI,
            result_model=ListType
        )

        vetted_results = vetting << (patch_content + "\n\n" + str(all_results.keys()))
        # Can hallucinate file paths, this works around it
        vet_dict = {k: all_results[k] if k in all_results else "" for k in vetted_results.items}

        # format string and send it to get summary
        formatted_str = self.prepare_summary(patch_content,vet_dict)

        summary = self.summaryAgent << formatted_str

        #post to github
        comment_url = self.post_to_github(summary)

        return comment_url

# Create an instance of the agent
pr_review_agent = PRReviewAgent()

if __name__ == "__main__":
    with open("PRChanges.patch", "r") as f:
        patch_content = f.read()
    
    # Run the agent
    print(pr_review_agent.generate(patch_content))
