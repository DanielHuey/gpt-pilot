import json
import re
import subprocess
import uuid
from utils.style import color_yellow, color_yellow_bold

from database.database import get_saved_development_step, save_development_step, delete_all_subsequent_steps
from helpers.exceptions.TokenLimitError import TokenLimitError
from utils.function_calling import parse_agent_response, FunctionCallSet
from utils.llm_connection import create_gpt_chat_completion
from utils.utils import array_of_objects_to_string, get_prompt, get_sys_message, capitalize_first_word_with_underscores
from logger.logger import logger
from prompts.prompts import ask_user
from const.llm import END_RESPONSE
from helpers.cli import running_processes


class AgentConvo:
    """
    Represents a conversation with an agent.

    Args:
        agent: An instance of the agent participating in the conversation.
    """
    def __init__(self, agent):
        # [{'role': 'system'|'user'|'assistant', 'content': ''}, ...]
        self.messages: list[dict] = []
        self.branches = {}
        self.log_to_user = True
        self.agent = agent
        self.high_level_step = self.agent.project.current_step

        # add system message
        system_message = get_sys_message(self.agent.role,self.agent.project.args)
        logger.info('\n>>>>>>>>>> System Prompt >>>>>>>>>>\n%s\n>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>',
                    system_message['content'])
        self.messages.append(system_message)

    def send_message(self, prompt_path=None, prompt_data=None, function_calls: FunctionCallSet = None):
        """
        Sends a message in the conversation.

        Args:
            prompt_path: The path to a prompt.
            prompt_data: Data associated with the prompt.
            function_calls: Optional function calls to be included in the message.

        Returns:
            The response from the agent.
        """
        # craft message
        self.construct_and_add_message_from_prompt(prompt_path, prompt_data)

        # TODO: move this if block (and the other below) to Developer agent - https://github.com/Pythagora-io/gpt-pilot/issues/91#issuecomment-1751964079
        # check if we already have the LLM response saved
        if self.agent.__class__.__name__ == 'Developer':
            self.agent.project.llm_req_num += 1
        development_step = get_saved_development_step(self.agent.project)
        if development_step is not None and self.agent.project.skip_steps:
            # if we do, use it
            print(color_yellow(f'Restoring development step with id {development_step.id}'))
            self.agent.project.checkpoints['last_development_step'] = development_step
            self.agent.project.restore_files(development_step.id)
            response = development_step.llm_response
            self.messages = development_step.messages

            if self.agent.project.skip_until_dev_step and str(development_step.id) == self.agent.project.skip_until_dev_step:
                self.agent.project.skip_steps = False
                delete_all_subsequent_steps(self.agent.project)

                if 'delete_unrelated_steps' in self.agent.project.args and self.agent.project.args['delete_unrelated_steps']:
                    self.agent.project.delete_all_steps_except_current_branch()

            if development_step.token_limit_exception_raised:
                raise TokenLimitError(development_step.token_limit_exception_raised)
        else:
            # if we don't, get the response from LLM
            try:
                response = create_gpt_chat_completion(self.messages, self.high_level_step, self.agent.project, function_calls=function_calls)
            except TokenLimitError as e:
                save_development_step(self.agent.project, prompt_path, prompt_data, self.messages, '', str(e))
                raise e

            # TODO: move this code to Developer agent - https://github.com/Pythagora-io/gpt-pilot/issues/91#issuecomment-1751964079
            if response != {} and self.agent.__class__.__name__ == 'Developer':
                development_step = save_development_step(self.agent.project, prompt_path, prompt_data, self.messages, response)

        # TODO handle errors from OpenAI
        # It's complicated because calling functions are expecting different types of responses - string or tuple
        # https://github.com/Pythagora-io/gpt-pilot/issues/165 & #91
        if response == {}:
            logger.error('Aborting with "OpenAI API error happened"')
            raise Exception("OpenAI API error happened.")

        response = parse_agent_response(response, function_calls)
        message_content = self.format_message_content(response, function_calls)

        # TODO we need to specify the response when there is a function called
        # TODO maybe we can have a specific function that creates the GPT response from the function call
        logger.info('\n>>>>>>>>>> Assistant Prompt >>>>>>>>>>\n%s\n>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>', message_content)
        self.messages.append({"role": "assistant", "content": message_content})
        self.log_message(message_content)

        return response

    def format_message_content(self, response, function_calls):
        # TODO remove this once the database is set up properly
        if isinstance(response, str):
            return response
        else:
            # string_response = []
            # for key, value in response.items():
            #     string_response.append(f'# {key}')
            #
            #     if isinstance(value, list):
            #         if 'to_message' in function_calls:
            #             string_response.append(function_calls['to_message'](value))
            #         elif len(value) > 0 and isinstance(value[0], dict):
            #             string_response.extend([
            #                 f'##{i}\n' + array_of_objects_to_string(d)
            #                 for i, d in enumerate(value)
            #             ])
            #         else:
            #             string_response.extend(['- ' + r for r in value])
            #     else:
            #         string_response.append(str(value))
            #
            # return '\n'.join(string_response)
            return json.dumps(response)
        # TODO END


    def continuous_conversation(self, prompt_path, prompt_data, function_calls=None):
        """
        Conducts a continuous conversation with the agent.

        Args:
            prompt_path: The path to a prompt.
            prompt_data: Data associated with the prompt.
            function_calls: Optional function calls to be included in the conversation.

        Returns:
            List of accepted messages in the conversation.
        """
        self.log_to_user = False
        accepted_messages = []
        response = self.send_message(prompt_path, prompt_data, function_calls)

        # Continue conversation until GPT response equals END_RESPONSE
        while response != END_RESPONSE:
            user_message = ask_user(self.agent.project, response,
                                    hint=color_yellow("Do you want to add anything else? If not, ") + color_yellow_bold('just press ENTER.'),
                                    require_some_input=False)

            if user_message == "":
                accepted_messages.append(response)

            logger.info('\n>>>>>>>>>> User Message >>>>>>>>>>\n%s\n>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>', user_message)
            self.messages.append({"role": "user", "content": user_message})
            response = self.send_message(None, None, function_calls)

        self.log_to_user = True
        return accepted_messages

    def save_branch(self, branch_name=None):
        if branch_name is None:
            branch_name = str(uuid.uuid4())
        self.branches[branch_name] = self.messages.copy()
        return branch_name

    def load_branch(self, branch_name, reload_files=True):
        self.messages = self.branches[branch_name].copy()
        if reload_files:
            # TODO make this more flexible - with every message, save metadata so every time we load a branch, reconstruct all messages from scratch
            self.replace_files()

    def replace_files(self):
        files = self.agent.project.get_all_coded_files()
        for msg in self.messages:
            if msg['role'] == 'user':
                for file in files:
                    self.replace_file_content(msg['content'], file['path'], file['content'])

    def replace_file_content(self, message, file_path, new_content):
        escaped_file_path = re.escape(file_path)

        pattern = rf'\*\*{{ {escaped_file_path} }}\*\*\n```\n(.*?)\n```'

        new_section_content = f'**{{ {file_path} }}**\n```\n{new_content}\n```'

        updated_message, num_replacements = re.subn(pattern, new_section_content, message, flags=re.DOTALL)

        if num_replacements == 0:
            return message

        return updated_message

    def convo_length(self):
        return len([msg for msg in self.messages if msg['role'] != 'system'])


    def log_message(self, content):
        """
        Logs a message in the conversation.

        Args:
            content: The content of the message to be logged.
        """
        print_msg = capitalize_first_word_with_underscores(self.high_level_step)
        if self.log_to_user:
            if self.agent.project.checkpoints['last_development_step'] is not None:
                print(color_yellow("\nDev step ") + color_yellow_bold(str(self.agent.project.checkpoints['last_development_step'])) + '\n', end='')
            print(f"\n{content}\n", type='local')
        logger.info(f"{print_msg}: {content}\n")

    def to_context_prompt(self):
        logger.info(f'to_context_prompt({self.agent.project.current_step})')

        # TODO: get dependencies & versions from the project (package.json, requirements.txt, pom.xml, etc.)
        # Ideally, the LLM could do this, and we update it on load & whenever the file changes
        # ...or LLM generates a script for `.gpt-pilot/get_dependencies` that we run
        # https://github.com/Pythagora-io/gpt-pilot/issues/189
        return get_prompt('development/context.prompt', {
            'directory_tree': self.agent.project.get_directory_tree(),
            'running_processes': running_processes,
        })

    def to_playground(self):
        with open('const/convert_to_playground_convo.js', 'r', encoding='utf-8') as file:
            content = file.read()
        process = subprocess.Popen('pbcopy', stdin=subprocess.PIPE)
        process.communicate(content.replace('{{messages}}', str(self.messages)).encode('utf-8'))

    def remove_last_x_messages(self, x):
        self.messages = self.messages[:-x]

    def construct_and_add_message_from_prompt(self, prompt_path, prompt_data):
        if prompt_path is not None and prompt_data is not None:
            prompt = get_prompt(prompt_path, prompt_data)
            logger.info('\n>>>>>>>>>> User Prompt >>>>>>>>>>\n%s\n>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>', prompt)
            self.messages.append({"role": "user", "content": prompt})
