"""A faithful port of how the legacy services read and write schedule files.

Source: ``ref files/BoilerCalander.py`` and ``ref files/TimerCalander.py`` (``*Task(json=...)``,
``*Calander(json=...)``, ``tojson``). It is used only as a test oracle: our output must survive this
reader, because the boiler and timer services on the boiler Pi still use it. Values stay strings, as
in the legacy code.
"""

Task = dict[str, str | int]


def parse(text: str) -> list[Task]:
    """Read a schedule file the way the legacy string-splitting parser does."""
    tasks: list[Task] = []
    for jtask in text.replace("[", "").replace("]", "").split("},{"):
        task: Task = {"day": 0, "hour": "00:00", "a": "0", "b": "0"}
        if jtask != "":  # the legacy constructor keeps its defaults for an empty piece
            for part in jtask.replace("{", "").replace("}", "").split(","):
                items = part.replace('"', "").split(":")
                if items[0] == "DayOfWeek":
                    task["day"] = int(items[1])
                elif items[0] == "Hour":
                    hour = items[1] + ":" + items[2]
                    if len(items) > 3:
                        hour = hour + ":" + items[3]
                    task["hour"] = hour
                elif items[0] in ("Target", "State"):
                    task["a"] = items[1]
                elif items[0] in ("Min", "RelayNum"):
                    task["b"] = items[1]
        tasks.append(task)
    return tasks


def write_boiler(tasks: list[Task]) -> str:
    """The legacy ``BoilerCalander.tojson``."""
    text = "["
    for task in tasks:
        text += (
            '{"DayOfWeek":'
            + str(task["day"])
            + ',"Hour":"'
            + str(task["hour"])
            + '","Target":'
            + str(task["a"])
            + ',"Min":'
            + str(task["b"])
            + "},"
        )
    return text[0 : len(text) - 1] + "]"


def write_timer(tasks: list[Task]) -> str:
    """The legacy ``TimerCalander.tojson``."""
    text = "["
    for task in tasks:
        text += (
            '{"DayOfWeek":'
            + str(task["day"])
            + ',"Hour":"'
            + str(task["hour"])
            + '","State":'
            + str(task["a"])
            + ',"RelayNum":'
            + str(task["b"])
            + "},"
        )
    return text[0 : len(text) - 1] + "]"
