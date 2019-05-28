from typing import List, Set

from beamngpy import Scenario

from dbtypes.beamng import DBVehicle
from dbtypes.criteria import TestCase
from dbtypes.scheme import Participant


def get_movement_mode_file_path(participant: Participant) -> str:
    """
    Returns the path of the file for storing the current movement mode of the given participant. The used path separator
    is '/' to allow to be used in lua files.
    :param participant: The participant to get the path for.
    :return: The path of the file for storing the movement mode of the given participant.
    """
    return participant.id + "_movementMode"


def enable_participant_movements(participants: List[Participant]) -> None:
    """
    Adds triggers to the scenario that set the next waypoints for the given participants. Must be called after adding
    the waypoints. Otherwise some IDs of waypoints may be None.
    :param participants: The participants to add movement changing triggers to
    """
    from util import add_to_prefab_file, eprint, get_lua_path
    from dbtypes.scheme import MovementMode
    from typing import Optional

    def generate_lua_av_command(participant: Participant, idx: int, next_mode: MovementMode,
                                current_mode: Optional[MovementMode] = None) -> List[str]:
        """
        NOTE When using this function the lua file where you include this command has to include the following line:
        local sh = require('ge/extensions/scenario/scenariohelper')
        """
        lua_av_command = []
        if next_mode is MovementMode.MANUAL:
            # FIXME Recognize speed (limits)
            if current_mode is not MovementMode.MANUAL:
                remaining_waypoints = "{'" + "', '".join(map(lambda w: w.id, participant.movement[idx + 1:])) + "'}"
                lua_av_command.extend([
                    "    sh.setAiRoute('" + participant.id + "', " + remaining_waypoints + ")"
                ])
        else:
            eprint("Mode " + str(next_mode) + " not supported, yet.")
        lua_av_command.extend([
            "    local modeFile = io.open('" + get_movement_mode_file_path(participant) + "', 'w')",
            "    modeFile:write('" + next_mode.value + "')",
            "    modeFile:close()"
        ])
        return lua_av_command

    lua_file = open(get_lua_path(), "w")
    lua_file.writelines([  # FIXME Is this needed somehow?
        "local M = {}\n",
        "local sh = require('ge/extensions/scenario/scenariohelper')",
        "\n",
        "local function onRaceStart()\n",
        "  print('onRaceStart called')\n"
    ])
    for participant in participants:
        for idx, waypoint in enumerate(participant.movement[0:1]):
            lua_file.writelines(generate_lua_av_command(participant, idx, waypoint.mode))
    lua_file.writelines([
        "end\n",
        "\n",
        "M.onRaceStart = onRaceStart\n",
        "return M"
    ])

    for participant in participants:
        current_movement_mode = None
        for idx, waypoint in enumerate(participant.movement[1:-1]):
            x_pos = waypoint.position[0]
            y_pos = waypoint.position[1]
            # NOTE Add further tolerance due to oversize of bounding box of the car compared to the actual body
            tolerance = waypoint.tolerance + 0.5

            def generate_lua_function() -> str:
                lua_lines = list()
                lua_lines.extend([
                    "local sh = require('ge/extensions/scenario/scenariohelper')",
                    "local function onWaypoint(data)",
                    "  if data['event'] == 'enter' then"
                ])
                lua_lines.extend(generate_lua_av_command(participant, idx, waypoint.mode, current_movement_mode))
                lua_lines.extend([
                    "  end",
                    "end",
                    "",
                    "return onWaypoint"
                ])
                return "\\r\\n".join(lua_lines)

            add_to_prefab_file([
                "new BeamNGTrigger() {",
                "    TriggerType = \"Sphere\";",
                "    TriggerMode = \"Overlaps\";",
                "    TriggerTestType = \"Race Corners\";",
                "    luaFunction = \"" + generate_lua_function() + "\";",
                "    tickPeriod = \"100\";",  # FIXME Think about it
                "    debug = \"0\";",
                "    ticking = \"0\";",  # FIXME Think about it
                "    triggerColor = \"255 192 0 45\";",
                "    defaultOnLeave = \"1\";",  # FIXME Think about it
                "    position = \"" + str(x_pos) + " " + str(y_pos) + " 0.5\";",
                "    scale = \"" + str(tolerance) + " " + str(tolerance) + " 10\";",
                "    rotationMatrix = \"1 0 0 0 1 0 0 0 1\";",
                "    mode = \"Ignore\";",
                "    canSave = \"1\";",  # FIXME Think about it
                "    canSaveDynamicFields = \"1\";",  # FIXME Think about it
                "};"
            ])

            current_movement_mode = waypoint.mode


def make_lanes_visible() -> None:
    """
    Workaround for making lanes visible.
    """
    from util import get_prefab_path
    prefab_file_path = get_prefab_path()
    prefab_file = open(prefab_file_path, "r")
    original_content = prefab_file.readlines()
    prefab_file.close()
    new_content = list()
    for line in original_content:
        if "overObjects" in line:
            new_line = line.replace("0", "1")
        else:
            new_line = line
        new_content.append(new_line)
    prefab_file = open(prefab_file_path, "w")
    prefab_file.writelines(new_content)
    prefab_file.close()


def control_avs(vehicles: List[DBVehicle]) -> None:
    from beamngpy import socket
    # TODO Check which AVs are in AUTONOMOUS or TRAINING mode
    # TODO Request AIs for request ids to get data for
    rids = [
        "position",
        "speed",
        "steeringAngle",
        "frontCamera",
        "lidar"
    ]
    for vehicle in vehicles:
        for rid in rids:
            print(rid)
            try:
                print(str(vehicle.poll_request(rid)))
            except socket.timeout:
                print("timeout when receiving requested data.")
            print()


def add_lap_config(waypoint_ids: Set[str]) -> None:
    """
    Adds a dummy lapConfig attribute to the scenario json to avoid nil value exceptions.
    """
    from util import add_to_json_file
    if not waypoint_ids == set():
        add_to_json_file([
            "        \"lapConfig\": [\"" + ("\", \"".join(waypoint_ids)) + "\"]"
        ])


def run_test_case(test_case: TestCase):
    from app import app
    from dbtypes.beamng import DBBeamNGpy
    from dbtypes.criteria import KPValue
    from shutil import rmtree
    import os
    home_path = app.config["BEAMNG_INSTALL_FOLDER"]
    user_path = app.config["BEAMNG_USER_PATH"]

    # Make sure there is no inference with previous tests while keeping the cache
    rmtree(os.path.join(user_path, "levels"), ignore_errors=True)

    # FIXME Determine port and host automatically. (Is it required to do so?)
    bng_instance = DBBeamNGpy('localhost', 64256, home=home_path, user=user_path)
    authors = ", ".join(test_case.authors)
    bng_scenario = Scenario(app.config["BEAMNG_LEVEL_NAME"], app.config["BEAMNG_SCENARIO_NAME"], authors=authors)
    test_case.scenario.add_all(bng_scenario)
    bng_scenario.make(bng_instance)

    # Make manual changes to the scenario files
    make_lanes_visible()
    # FIXME As long as manually inserting text it can only be called after make
    test_case.scenario.add_waypoints_to_scenario(bng_scenario)
    enable_participant_movements(test_case.scenario.participants)
    waypoints = set()
    for wps in [p.movement for p in test_case.scenario.participants]:
        for wp in wps:
            if wp.id is not None:  # FIXME Not all waypoints are added
                waypoints.add(wp.id)
    add_lap_config(waypoints)

    bng_instance.open(launch=True)
    try:
        bng_instance.load_scenario(bng_scenario)
        bng_instance.set_steps_per_second(test_case.stepsPerSecond)
        bng_instance.set_deterministic()
        bng_instance.hide_hud()
        bng_instance.start_scenario()
        bng_instance.pause()

        vehicles = [bng_scenario.get_vehicle(participant.id) for participant in test_case.scenario.participants]

        precondition = test_case.precondition_fct(bng_scenario)
        failure = test_case.failure_fct(bng_scenario)
        success = test_case.success_fct(bng_scenario)
        test_case_result = "undetermined"
        while test_case_result == "undetermined":
            if precondition.eval() is KPValue.FALSE:
                test_case_result = "skipped"
            elif failure.eval() is KPValue.TRUE:
                test_case_result = "failed"
            elif success.eval() is KPValue.TRUE:
                test_case_result = "succeeded"
            else:
                # test_case_result = "undetermined"
                for vehicle in vehicles:
                    bng_instance.poll_sensors(vehicle)  # Update sensor cache before controlling AVs
                control_avs(vehicles)
                bng_instance.step(test_case.aiFrequency)
        print("Test case result: " + test_case_result)
    finally:
        bng_instance.close()
