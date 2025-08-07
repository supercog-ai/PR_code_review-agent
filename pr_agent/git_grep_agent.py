from typing import List, Dict
import subprocess

# The actual sub-agent that runs git grep and returns structured results 
class GitGrepAgent():
    def run_git_grep(self, query: str) -> List[str]:
        # Runs "git grep -n <query>" for the given query to find exact matches in the codebase
        # parses each result line into (file_path, matched_line) both of which are strs
        #  and returns a list of (file_path, matched_line) tuples
        try:
            result = subprocess.run(
                        ["git", "grep", "-n", query],               # make sure that query is getting passed by the Main Agent!!!
                        capture_output=True,
                        text=True,
                        check=False,
                        encoding="utf-8"
            )


            # example git grep output: "code_rag_agent.py:6:from agentic.tools.rag_tool import RAGTool"


            # TODO: need to determine if the line number is neccessary returning...
            matches = [] # list of matches from the git grep command --> will hold all (file_path, matched_line) tuples found!
            if result.stdout:
                for line in result.stdout.splitlines():             
                    if not line:
                        continue
                    parts = line.split(":", 2)  # file_path, line_number, line_text
                    if len(parts) == 3:         # if the output line is in the correct format 
                        file_path = parts[0]
                        matches.append(file_path)
            return matches
        except Exception as e:
            print(f"Error running git grep: {e}")
            return {}
        

    # the entry point for running one turn (input -> processing -> output)
    def get_search(self, search_query: str) -> Dict[str, str]:
        grep_results = self.run_git_grep(search_query)                              # runs git grep for that specific query 


        # loops over each grep match
        files = {}
        for file_path in grep_results:
            if file_path not in files.keys():
                try:
                    with open(file_path) as file:       # this gives structural context for the matched file
                        file_contents = file.read()
                        files[file_path] = file_contents
                except:
                    pass

        return files
