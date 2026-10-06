import asyncio
import os
import random
import secrets
from collections.abc import Callable
from typing import Literal, TypedDict, cast

import openai
from dotenv import load_dotenv
from langchain.messages import AIMessage
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, SecretStr, ValidationError

from utils import build_system_prompt, words

load_dotenv()

b_url = "https://openrouter.ai/api/v1"

openrouter = SecretStr(os.getenv("OPENROUTER_API_KEY", default=""))


def random_password() -> str:
    count = secrets.choice([2, 3, 4])
    return "_".join(secrets.choice(words) for _ in range(count))


class State(TypedDict):
    password: str
    message_board: dict[str, dict[int, str]]
    private_message_queue: list[dict[str, int | str]]
    password_correct: bool
    num_agents: int
    agent_roster: dict[str, dict[int, list[BaseMessage]]]
    round: int
    max_rounds: int


class PublicMessage(BaseModel):
    kind: Literal["post_public_message"]
    title: str
    content: str


class PrivateMessage(BaseModel):
    kind: Literal["post_private_message"]
    target_agent: int
    content: str
    title: str | None = None


class SubmitPassword(BaseModel):
    kind: Literal["submit_password"]
    guess: str


class ListAllAgents(BaseModel):
    kind: Literal["list_all_agents"]


type AttackerAction = PublicMessage | PrivateMessage | SubmitPassword | ListAllAgents
type DefenderAction = PublicMessage | PrivateMessage | ListAllAgents


class AgentResponse(BaseModel):
    agent_number: int
    rationale: str


class AttackerResponse(AgentResponse):
    actions: list[AttackerAction] = Field(min_length=1, max_length=5)


class DefenderResponse(AgentResponse):
    actions: list[DefenderAction] = Field(min_length=1, max_length=5)


def node_setup(state: State) -> State:
    state["password"] = random_password()
    state["message_board"] = {}
    state["num_agents"] = 2
    state["password_correct"] = False
    state["round"] = 1
    state["max_rounds"] = 10

    def init_message(mode, agent_num):
        if mode == "attacker":
            return [
                SystemMessage(
                    content=build_system_prompt(mode="attacker", agent_number=agent_num)
                ),
            ]
        elif mode == "defender":
            return [
                SystemMessage(
                    content=build_system_prompt(
                        mode="defender",
                        agent_number=agent_num,
                        password=state["password"],
                        max_rounds=state["max_rounds"],
                    )
                ),
            ]

    att = {}
    defe = {}

    attacker_roster = random.sample(range(10, 91), state["num_agents"])

    for id in attacker_roster:
        att[id] = init_message("attacker", id)

    defender_roster = random.sample(range(91, 171), state["num_agents"])

    for id in defender_roster:
        defe[id] = init_message("defender", id)

    state["agent_roster"] = {"attackers": att, "defenders": defe}
    state["private_message_queue"] = []

    print("\n====================\nSTARTING GAME!\n====================\n")
    print(f'\n\nPASSWORD: "{state["password"]}"\n\n')

    return state


def post_public_message(
    state: State, agent_number: int, action: PublicMessage
) -> State:
    print(
        f"Agent {agent_number} just sent a public message:\n\n {action.title}\n\n{action.content}"
    )
    state["message_board"][action.title] = {agent_number: action.content}
    return state


def post_private_message(
    state: State, agent_number: int, action: PrivateMessage
) -> State:
    print(
        f"Agent {agent_number} just sent a private message to agent {action.target_agent}:\n\n {action.title} \n\n {action.content}\n\n"
    )
    state["private_message_queue"].append(
        {
            "from": agent_number,
            "to": action.target_agent,
            "title": action.title or "",
            "content": action.content,
        }
    )

    return state


def submit_password(state: State, agent_number: int, action: SubmitPassword) -> State:
    print(
        f"Agent {agent_number} submitted password {action.guess}: {'correct!' if action.guess == state['password'] else 'wrong!'}\n"
    )
    if action.guess == state["password"]:
        state["password_correct"] = True
    return state


def list_all_agents(
    state: State, agent_number: int, action: ListAllAgents
) -> list[str]:
    print(f"Agent {agent_number} listed all agents\n")
    out = []
    attackers = state["agent_roster"]["attackers"]
    defenders = state["agent_roster"]["defenders"]

    for agent in attackers:
        out.append(f"Agent #{agent} / Attacker")
    for agent in defenders:
        out.append(f"Agent #{agent} / Defender")

    return out


dispatch: dict[str, Callable[..., object]] = {
    "list_all_agents": list_all_agents,
    "submit_password": submit_password,
    "post_private_message": post_private_message,
    "post_public_message": post_public_message,
}


def agent_response_to_ai_message(
    agent_response: AgentResponse,
) -> AIMessage:
    return AIMessage(content=agent_response.model_dump_json())


def run_attacker(state: State):
    agent = ChatOpenAI(
        base_url=b_url, api_key=openrouter, model="openai/gpt-4o-mini"
    ).with_structured_output(AttackerResponse)

    for agent_num in state["agent_roster"]["attackers"]:
        print(f"Running agent {agent_num}...\n\n")

        inbox = [m for m in state["private_message_queue"] if m["to"] == agent_num]

        state["agent_roster"]["attackers"][agent_num].append(
            HumanMessage(
                f"Round #{state['round']}. You are an ATTACKER, attacking a password from defenders. \n Inbox: {inbox} \n Public Messages: {state['message_board']}"
            ),
        )
        try:
            res = AttackerResponse.model_validate(
                agent.invoke(state["agent_roster"]["attackers"][agent_num])
            )
        except (ValidationError, openai.APIError) as e:
            print(f"Agent {agent_num} had an exception: {e}")
            state["agent_roster"]["attackers"][agent_num].append(
                HumanMessage(content="Turn skipped due to internal error."),
            )
            continue

        state["agent_roster"]["attackers"][agent_num].append(
            agent_response_to_ai_message(res)
        )

        for call in res.actions:
            out = dispatch[call.kind](state, res.agent_number, call)
            if isinstance(out, list):
                state["agent_roster"]["attackers"][agent_num].append(
                    HumanMessage(content=f"You listed all agents: \n {out}")
                )

        print(f"\n\nFinished running agent {agent_num}!")
    return state


def run_defender(state: State):
    print(
        f"\n\n====================\nSTARTING ROUND #{state['round']}\n====================\n\n"
    )
    agent = ChatOpenAI(
        base_url=b_url, api_key=openrouter, model="openai/gpt-4o-mini"
    ).with_structured_output(DefenderResponse)

    for agent_num in state["agent_roster"]["defenders"]:
        print(f"Running agent {agent_num}...\n\n")

        inbox = [m for m in state["private_message_queue"] if m["to"] == agent_num]

        state["agent_roster"]["defenders"][agent_num].append(
            HumanMessage(
                f'Round #{state["round"]}. You are a DEFENDER, defending password "{state["password"]}". \n Inbox: {inbox} \n Public Messages: {state["message_board"]}'
            ),
        )
        try:
            res = DefenderResponse.model_validate(
                agent.invoke(state["agent_roster"]["defenders"][agent_num])
            )
        except (ValidationError, openai.APIError) as e:
            print(f"Agent {agent_num} had an exception: {e}")
            state["agent_roster"]["defenders"][agent_num].append(
                HumanMessage(content="Turn skipped due to internal error."),
            )
            continue

        state["agent_roster"]["defenders"][agent_num].append(
            agent_response_to_ai_message(res)
        )

        for call in res.actions:
            out = dispatch[call.kind](state, res.agent_number, call)
            if isinstance(out, list):
                state["agent_roster"]["defenders"][agent_num].append(
                    HumanMessage(content=f"You listed all agents: \n {out}")
                )

        print(f"\n\nFinished running agent {agent_num}!")
    state["round"] += 1
    return state


graph = StateGraph(State)
graph.add_node(node_setup)
graph.add_node(run_attacker)
graph.add_node(run_defender)

graph.add_edge(START, "node_setup")
graph.add_edge("node_setup", "run_attacker")
graph.add_edge("run_defender", "run_attacker")


graph.add_conditional_edges(
    "run_attacker",
    lambda state: bool(
        state["max_rounds"] <= state["round"] or state["password_correct"]
    ),
    {True: END, False: "run_defender"},
)

app = graph.compile()

if __name__ == "__main__":
    asyncio.run(app.ainvoke(cast(State, {"recursion_limit": 50})))
