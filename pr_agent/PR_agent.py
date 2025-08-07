import os
import requests
from typing import List
from pydantic import Field, BaseModel
from dotenv import load_dotenv
from agentic.common import Agent
from agentic.models import GPT_4O_MINI
from git_grep_agent import GitGrepAgent
from summary_agent import SummaryAgent
from pydantic import BaseModel
from typing import Dict, List, Any, Generator, Optional, Tuple
from agentic.common import Agent, AgentRunner, ThreadContext
from agentic.events import Event, ChatOutput, TurnEnd, PromptStarted, Prompt

load_dotenv()

def call_llm(input):
    agent, text = input
    return agent._get_llm_completion(history=[{"role": "user", "content": text}], thread_context=agent.thread_context, model_override=None, stream=False).choices[0].message.content

class SearchResult(BaseModel):
    query: str = Field(
        description="Query used in this search."
    )
    file_path: str = Field(
        description="Path of the file this code/documentation belongs to."
    )
    content: str = Field(
        description="Content returned from search."
    )
    included_defs: List[str] = Field(
        default_factory=list,
        desciption="Similarity score returned from vector search."
    )

class Searches(BaseModel):
    searches: List[str] = Field(
        description="Search queries."
    )

class RelevanceResult(BaseModel):
    relevant: bool 

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

Output the result as a JSON array of string patterns suitable for use with git grep, such as:

Be strict: if a symbol was defined in the patch, do not include it.""",
            model=GPT_4O_MINI,
            result_model=Searches,
        )

        self.relevanceAgent = Agent(
            name="Code Relevance Agent",
            instructions="""You are a code analysis agent. You will be given two inputs:

A patch file (diff) representing changes made to one or more source code files.

A single excerpt of text that was returned from git grep, which includes a matching line of text and its file path.

Your task is to determine whether the excerpt is relevant to the changes in the patch. Relevance is defined by either of the following:

The file path in the excerpt matches (exactly or closely) any file modified in the patch.

The content of the excerpt is conceptually or semantically related to the code changes in the patch (e.g., it references a function, variable, or concept that was added, modified, or removed).

Be conservative: if there's insufficient evidence for relevance, return false.""",
            model=GPT_4O_MINI,
            result_model=RelevanceResult,
        )

        self.summaryAgent = SummaryAgent()

    def prepare_summary(self, patch_content: str, filtered_results: List[SearchResult]) -> str:
        """Prepare for summary agent"""
        formatted_str = ""
        formatted_str += f"<Patch file>\n"
        formatted_str += f"{patch_content}\n"
        formatted_str += f"</Patch File>\n\n"
        
        for result in filtered_results:
            formatted_str += f"<{result.file_path}>\n"
            formatted_str += f"{result.content}\n"
            formatted_str += f"</{result.file_path}>\n\n"

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

    def next_turn(
        self,
        request: str,
        request_context: dict = None,
        request_id: str = None,
        continue_result: dict = {},
        debug = "",
    ) -> Generator[Event, Any, None]:
        
        query = request.payload if isinstance(request, Prompt) else request
        yield PromptStarted(query, {"query": query})
        
        # Generate search queries
        queries = yield from self.queryAgent.final_result(
            request_context.get("patch_content"),
            request_context={
                "thread_id": request_context.get("thread_id")
            }
        )

        print("queries: "+str(queries))

        # RAG and Git-Grep queries 
        all_results = {}
        for query in queries.searches[:10]:
            searchResponse = self.git_grep_agent.get_search(query)
            
            # Process each result
            for file, result in searchResponse.sections.items():
                if not file in all_results:
                    all_results[file] = SearchResult(query=query,file_path=result.file_path,content=result.search_result,similarity_score=result.similarity_score,included_defs=result.included_defs)
            
            searchResponse = yield from self.git_grep_agent.final_result(
                f"Search codebase with git grep",
                request_context={
                    "query": query,
                    "thread_id": request_context.get("thread_id")
                }
            )
          
            # Process each result
            # grep_response.sections is a list of CodeSection objects
            for file, result in searchResponse.sections.items():
                if not file in all_results:
                    all_results[file] = SearchResult(
                        query=query,
                        file_path=result.file_path,
                        content=result.search_result,
                        similarity_score=result.similarity_score,
                        included_defs=result.included_defs
            )


        print("all: "+str(all_results))

        # Filter search results using LLM-based relevance checking
        filtered_results = []
        for result in all_results.values(): 
            
            try:
                relevance_check = self.relevanceAgent << f"<Patch File>\n{patch_content}\n</Patch File>\n\n<Content>{result.content}</Content><Query>{result.query}</Query>"
                self.relevanceAgent.reset_history()
                if relevance_check.relevant:
                    filtered_results.append(result)
            except Exception as e:
                # LLM error
                print(e)

        formatted_str = self.prepare_summary(patch_content,filtered_results)

        summary = self.summaryAgent << formatted_str

        comment_url = self.post_to_github(summary)

        return comment_url

# Create an instance of the agent
pr_review_agent = PRReviewAgent()

if __name__ == "__main__":
    # Change to PRChangesTest.patch for testing
    with open("PRChanges.patch", "r") as f:
        patch_content = f.read()
    
    # Run the agent
    print(pr_review_agent.generate(patch_content))