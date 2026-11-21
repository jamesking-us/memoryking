"""OpenAI-compatible function calling: the lean tool definitions and a dispatcher.

No third-party package and no model. This script plays the part of the model by choosing the
tool calls itself and prints what a model would see back.
"""

import json

from demo_store import demo_store

from memoryking.integrations.function_calling import FunctionTools


def main():
    with demo_store() as (client, _server, _credential):
        memory = FunctionTools(client)

        # What you pass to your provider as `tools=`; use style="responses" for the flat shape.
        for definition in memory.definitions():
            function = definition["function"]
            print("tool:", function["name"], "-", function["description"][:60] + "...")

        # Where a real loop plugs in: send `memory.definitions()` with the conversation, and for
        # every tool call the model returns, append a tool message whose content is
        #   memory.call(call.function.name, call.function.arguments)
        # The call never raises on a refusal; the model gets {"error": ..., "hint": ...}.
        planned = [
            ("remember", {"text": "Raising every timeout exceeded the retry deadline."}),
            ("recall", {"query": "timeouts"}),
            (
                "save_progress",
                {
                    "task": "retry-fix",
                    "objective": "Fix the deadline",
                    "current_state": "Investigating",
                },
            ),
            ("resume", {"task": "retry-fix"}),
            ("resume", {"task": "no-such-task"}),
        ]
        for name, arguments in planned:
            output = memory.call(name, json.dumps(arguments))
            print(
                f"\n> {name}({json.dumps(arguments)[:70]})\n{chr(10).join(line[:110] for line in output.splitlines()[:3])}"
            )
    print("\nFunction-calling example passed.")


if __name__ == "__main__":
    main()
