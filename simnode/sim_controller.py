from logging import getLogger
from socket import socket
from threading import Lock
from typing import List, Set, Optional, Tuple, Callable

from beamngpy import Scenario
from drivebuildclient import static_vars
from drivebuildclient.aiExchangeMessages_pb2 import SimulationID

from dbtypes import ExtThread
from dbtypes.beamngpy import DBBeamNGpy
from dbtypes.criteria import TestCase, KPValue, CriteriaFunction
from dbtypes.scheme import Participant, MovementMode

_logger = getLogger("DriveBuild.SimNode.SimController")


class Simulation:
    def __init__(self, sid: SimulationID, pickled_test_case: bytes, port: int):
        import dill as pickle
        self.sid = sid
        self._sim_name = "drivebuild_" + sid.sid
        self.serialized_sid = sid.SerializeToString()
        self.pickled_test_case = pickled_test_case
        test_case = pickle.loads(pickled_test_case)
        self.test_name = test_case.name
        self.port = port
        self._sim_server_socket = None
        self._sim_node_client_socket = None

    def start_server(self, handle_simulation_message: Callable[[socket, Tuple[str, int]], None]) -> None:
        from threading import Thread
        from drivebuildclient import accept_at_server, create_server
        if self._sim_server_socket:
            raise ValueError("The simulation already started a server at " + str(self.port))
        else:
            # print("Create server at " + str(self.port))
            self._sim_server_socket = create_server(self.port)
            simulation_sim_node_com_server = Thread(target=accept_at_server,
                                                    args=(self._sim_server_socket, handle_simulation_message))
            simulation_sim_node_com_server.daemon = True
            simulation_sim_node_com_server.start()

    def send_message_to_sim_node(self, action: bytes, data: List[bytes]) -> bytes:
        from drivebuildclient import send_request, create_client
        from time import sleep
        while not self._sim_node_client_socket:
            try:
                self._sim_node_client_socket = create_client("localhost", self.port)
            except ConnectionRefusedError:
                retry_delay = 5
                _logger.debug("Retry creating client connection in " + str(retry_delay) + " seconds.")
                sleep(retry_delay)
        result = send_request(self._sim_node_client_socket, action, data)
        return result

    def _get_movement_mode_file_path(self, pid: str, in_lua: bool) -> str:
        """
        Returns the path of the file for storing the current movement mode of the given participant. The used path separator
        is '/' to allow to be used in lua files.
        :param pid: The participant id to get the path for.
        :param in_lua: Whether the resulting path should be appropriate for lua or python (True=Lua, False=Python).
        :return: The path of the file for storing the movement mode of the given participant.
        """
        import os
        return os.path.join("" if in_lua else self.get_user_path(), pid + "_movementMode")

    def get_current_movement_mode(self, pid: str) -> Optional[MovementMode]:
        import os
        mode_file_path = self._get_movement_mode_file_path(pid, False)
        if os.path.exists(mode_file_path):
            mode_file = open(mode_file_path, "r")
            mode = MovementMode[mode_file.readline()]
            mode_file.close()
            return mode
        else:
            return None

    def _generate_lua_av_command(self, participant: Participant, idx: int, next_mode: MovementMode) -> List[str]:
        """
        NOTE When using this function the lua file where you include this command has to include the following lines:
        local sh = require('ge/extensions/scenario/scenariohelper')
        local ve = ...
        local ai = ...
        NOTE Pass -1 as idx when passing mode of the initial state of the participant
        """
        """
        NOTE sh.setAiPath(...) and sh.setAiRoute(...) require to place waypoints in the middle of the lanes
        otherwise BeamNG may show an error (on the GUI -> So hud needs to be enabled) that "There is no path
        from X to Y". 
        """
        # NOTE setAiPath/setAiRoute: BeamNG allows to EITHER set a target speed or a speed limit
        # NOTE sh.setAiLine(...) is a custom function introduced into BeamNG
        from dbtypes.scheme import WayPoint
        lua_av_command = [
            "    local modeFile = io.open('" + self._get_movement_mode_file_path(participant.id, True) + "', 'w')",
            "    modeFile:write('" + next_mode.value + "')",
            "    modeFile:close()"
        ]
        remaining_waypoints = participant.movement[idx + 1:]
        if remaining_waypoints:
            current_waypoint = participant.initial_state if idx < 0 else participant.movement[idx]
            speed_limit = current_waypoint.speed_limit if remaining_waypoints else None
            target_speed = current_waypoint.target_speed if remaining_waypoints else None
            if speed_limit:
                speed_param = ", routeSpeed=" + str(speed_limit) + ", routeSpeedMode='limit'"
            elif target_speed:
                speed_param = ", routeSpeed=" + str(target_speed) + ", routeSpeedMode='set'"
            else:
                speed_param = ""
            if next_mode == MovementMode._BEAMNG:
                serialized_waypoints = "{'" + "', '".join([wp.id for wp in remaining_waypoints]) + "'}"
                lua_av_command.append(
                    "    sh.setAiPath({vehicleName='" + participant.id + "', waypoints=" + serialized_waypoints
                    + ", driveInLane='on'" + speed_param + "})"
                )
            elif next_mode in [MovementMode.MANUAL, MovementMode.TRAINING]:
                while len(remaining_waypoints) < 3:  # NOTE At least 3 waypoints have to be passed to setAiRoute(...)
                    remaining_waypoints.append(remaining_waypoints[-1])

                def _waypoint_to_tuple(waypoint: WayPoint) -> str:
                    return "{" + ", ".join([str(waypoint.position[0]), str(waypoint.position[1]), "0"]) + "}"

                ai_line = "{" + ", ".join(["{pos=" + _waypoint_to_tuple(w) + "}" for w in remaining_waypoints]) + "}"
                ai_path_command = "    sh.setAiLine('" + participant.id + "', {line=" + ai_line + speed_param + "})"
                lua_av_command.extend([ai_path_command])
            elif next_mode == MovementMode.AUTONOMOUS:
                lua_av_command.extend([
                    "    sh.setAiMode('" + participant.id + "', 'disabled')"  # Disable previous calls to sh.setAiRoute
                ])
            else:
                _logger.warning("Can not handle MovementMode " + str(next_mode) + ".")
        return lua_av_command

    def get_user_path(self) -> str:
        return self._bng_instance.user.as_posix()

    def _get_lua_path(self) -> str:
        import os
        return os.path.join(
            self._get_scenario_dir_path(),
            self._sim_name + ".lua"
        )

    def _get_scenario_dir_path(self) -> str:
        from config import BEAMNG_LEVEL_NAME
        import os
        return os.path.join(
            self.get_user_path(),
            "levels",
            BEAMNG_LEVEL_NAME,
            "scenarios"
        )

    def _get_prefab_path(self) -> str:
        import os
        return os.path.join(
            self._get_scenario_dir_path(),
            self._sim_name + ".prefab"
        )

    def _get_json_path(self) -> str:
        import os
        return os.path.join(
            self._get_scenario_dir_path(),
            self._sim_name + ".json"
        )

    def _add_to_prefab_file(self, new_content: List[str]) -> None:
        """
        Workaround for adding content to a scenario prefab if there is no explicit method for it.
        :param new_content: The lines of content to add.
        """
        prefab_file_path = self._get_prefab_path()
        prefab_file = open(prefab_file_path, "r")
        original_content = prefab_file.readlines()
        prefab_file.close()
        for line in new_content:
            original_content.insert(-2, line + "\n")
        prefab_file = open(prefab_file_path, "w")
        prefab_file.writelines(original_content)
        prefab_file.close()

    def _add_to_json_file(self, new_content: List[str]) -> None:
        """
        Workaround for adding content to a scenario json if there is no explicit method for it.
        :param new_content: The lines of content to add.
        """
        json_file_path = self._get_json_path()
        json_file = open(json_file_path, "r")
        original_content = json_file.readlines()
        json_file.close()
        original_content[-3] = original_content[-3] + ",\n"  # Make sure previous line has a comma
        for line in new_content:
            original_content.insert(-2, line + "\n")
        json_file = open(json_file_path, "w")
        json_file.writelines(original_content)
        json_file.close()

    def _generate_lua_file(self, participants: List[Participant]) -> None:
        lua_file = open(self._get_lua_path(), "w")
        lua_file.writelines([  # FIXME Is this needed somehow?
            "local M = {}\n",
            "local sh = require('ge/extensions/scenario/scenariohelper')\n",
            "\n",
            "local function onRaceStart()\n",
        ])
        for participant in participants:
            lua_file.writelines(
                map(lambda l: l + "\r\n",
                    self._generate_lua_av_command(participant, -1, participant.initial_state.mode)))
        lua_file.writelines([
            "end\n",
            "\n",
            "M.onRaceStart = onRaceStart\n",
            "return M"
        ])
        lua_file.close()

    def _add_lua_triggers(self, participants: List[Participant]) -> None:
        for participant in participants:
            for idx, waypoint in enumerate(participant.movement[0:-1]):
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
                    lua_lines.extend(
                        self._generate_lua_av_command(participant, idx, waypoint.mode))
                    lua_lines.extend([
                        "  end",
                        "end",
                        "",
                        "return onWaypoint"
                    ])
                    return "\\r\\n".join(lua_lines)

                self._add_to_prefab_file([
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

    def _enable_participant_movements(self, participants: List[Participant]) -> None:
        """
        Adds triggers to the scenario that set the next waypoints for the given participants. Must be called after adding
        the waypoints. Otherwise some IDs of waypoints may be None.
        :param participants: The participants to add movement changing triggers to
        """
        self._generate_lua_file(participants)
        self._add_lua_triggers(participants)

    @static_vars(render_priorities={"asphalt_01_a": 10, "line_white": 9, "line_yellow": 9})
    def _make_lanes_visible(self) -> None:
        """
        Workaround for making lanes visible and rendered correctly.
        """
        prefab_file_path = self._get_prefab_path()
        prefab_file = open(prefab_file_path, "r")
        original_content = prefab_file.readlines()
        prefab_file.close()

        new_content = list()

        def _add_replaced_lane_properties(material: str) -> None:
            if material in ["road_rubber_sticky"]:
                new_content.append("renderPriority = \"10\";\n")
                new_content.append("textureLength = \"2.5\";\n")
                new_content.append("distanceFade = \"1000 1000\";\n")
                new_content.append("drivability = \"1\";\n")
            elif material in ["line_white", "line_yellow", "line_dashed_short", "line_yellow_double"]:
                new_content.append("renderPriority = \"9\";\n")
                new_content.append("textureLength = \"16\";\n")
                new_content.append("distanceFade = \"0 0\";\n")
                new_content.append("drivability = \"-1\";\n")

        in_lane_segment = False
        for line in original_content:
            new_line = line
            if "new DecalRoad" in line:
                in_lane_segment = True
            elif "};" in line:
                in_lane_segment = False
            elif in_lane_segment:
                if "overObjects" in line:  # NOTE Make sure lanes are visible
                    new_line = line.replace("0", "1")
                elif "improvedSpline" in line:  # NOTE Make sure markings are drawn nicely
                    new_line = line.replace("1", "0")
                elif "renderPriority" in line \
                        or "textureLength" in line \
                        or "distanceFade" in line \
                        or "drivability" in line:  # Remove lane properties to be replaced
                    new_line = ""
                elif "Material" in line:  # Add custom material specific lines
                    material = line.split("=")[1].strip()[1:-2]
                    _add_replaced_lane_properties(material)
            new_content.append(new_line)
        prefab_file = open(prefab_file_path, "w")
        prefab_file.writelines(new_content)
        prefab_file.close()

    def _annotate_objects(self) -> None:
        prefab_file_path = self._get_prefab_path()
        prefab_file = open(prefab_file_path, "r")
        original_content = prefab_file.readlines()
        prefab_file.close()
        new_content = list()
        for line in original_content:
            if "overObjects" in line:
                new_content.append("annotation = \"STREET\";\n")
            new_content.append(line)
        prefab_file = open(prefab_file_path, "w")
        prefab_file.writelines(new_content)
        prefab_file.close()

    def _request_control_avs(self, vids: List[str]) -> None:
        from drivebuildclient.aiExchangeMessages_pb2 import VehicleID
        import dill as pickle
        for v in vids:
            # print(self.sid.sid + ": Request control for " + v)
            mode = self.get_current_movement_mode(v)
            if not mode:  # If there is no movement mode file assume participant is still in mode of initial state
                test_case = pickle.loads(self.pickled_test_case)
                mode = [p.initial_state.mode for p in test_case.scenario.participants if p.id == v][0]
            if mode in [MovementMode.AUTONOMOUS, MovementMode.TRAINING, MovementMode._BEAMNG]:
                vid = VehicleID()
                vid.vid = v
                message = self.send_message_to_sim_node(b"requestAiFor", [self.serialized_sid, vid.SerializeToString()])
                _logger.debug(message)
            elif mode == MovementMode.MANUAL:
                pass  # No AI to request
            else:
                _logger.warning(
                    self.sid.sid + ":" + v + ": Can not handle movement mode " + (mode.name if mode else "None"))

    def _add_lap_config(self, waypoint_ids: Set[str]) -> None:
        """
        Adds a dummy lapConfig attribute to the scenario json to avoid nil value exceptions. This call makes waypoints
        visible. Without adding a lapConfig calls to functions like setAiPath(...) fail at least when called during
        onRaceStart(...).
        """
        if not waypoint_ids == set():
            self._add_to_json_file([
                "        \"lapConfig\": [\"" + ("\", \"".join(waypoint_ids)) + "\"]"
            ])

    def _add_waypoints_to_scenario(self, participants: List[Participant]) -> None:
        """
        This method is only needed until generator.py::add_waypoints_to_scenario can be implemented.
        NOTE: This method has to be called after scenario.make()
        """
        for participant in participants:
            wp_prefix = "wp_" + participant.id + "_"
            counter = 0
            for waypoint in participant.movement:
                if waypoint.id is None:
                    waypoint.id = wp_prefix + str(counter)
                    counter += 1
                tolerance = str(waypoint.tolerance)
                self._add_to_prefab_file([
                    "new BeamNGWaypoint(" + waypoint.id + "){",
                    "   drawDebug = \"0\";",
                    "   directionalWaypoint = \"0\";",  # FIXME Should I use directional waypoints?
                    "   position = \"" + str(waypoint.position[0]) + " " + str(waypoint.position[1]) + " 0\";",
                    "   scale = \"" + tolerance + " " + tolerance + " " + tolerance + "\";",
                    "   rotationMatrix = \"1 0 0 0 1 0 0 0 1\";",
                    "   mode = \"Ignore\";",  # FIXME Which mode is suitable?
                    "   canSave = \"1\";",  # FIXME Think about it
                    "   canSaveDynamicFields = \"1\";",  # FIXME Think about it
                    "};"
                ])

    @staticmethod
    def _is_port_available(port: int) -> bool:
        from socket import socket, AF_INET, SOCK_STREAM
        sock = socket(AF_INET, SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', port))
        is_open = (result != 0)
        sock.close()
        return is_open

    def get_verification(self) -> Tuple[CriteriaFunction, CriteriaFunction, CriteriaFunction]:
        """
        Returns precondition, failure and success function.
        """
        import dill as pickle
        test_case: TestCase = pickle.loads(self.pickled_test_case)
        return test_case.precondition_fct, test_case.failure_fct, test_case.success_fct

    def _run_runtime_verification(self, ai_frequency: int) -> None:
        from drivebuildclient.aiExchangeMessages_pb2 import TestResult, VehicleIDs, Num, Bool
        from config import TIMEOUT
        from datetime import datetime
        from threading import Thread
        from queue import Queue

        def _get_verification() -> Tuple[KPValue, KPValue, KPValue]:
            from drivebuildclient.aiExchangeMessages_pb2 import VerificationResult
            # FIXME Determine appropriate timeout
            response = self.send_message_to_sim_node(b"verify", [self.serialized_sid])
            if response:
                verification = VerificationResult()
                verification.ParseFromString(response)
                return KPValue[verification.precondition], KPValue[verification.failure], KPValue[verification.success]
            else:
                _logger.warning("Verification of criteria at simulation " + self._sim_name + " timed out.")
                return KPValue.UNKNOWN, KPValue.UNKNOWN, KPValue.UNKNOWN

        def _run_verification_cycles(result_queue: Queue) -> None:
            test_case_result: TestResult.Result = TestResult.Result.UNKNOWN
            while test_case_result is TestResult.Result.UNKNOWN and (
                    datetime.now() - test_start_time).seconds < TIMEOUT:
                is_running = Bool()
                is_running.ParseFromString(self.send_message_to_sim_node(b"isRunning", [self.serialized_sid]))
                if is_running.value:
                    cycle_start_time = datetime.now()
                    self.send_message_to_sim_node(b"pollSensors", [self.serialized_sid])
                    # print(self.sid.sid + ": Polled sensors")
                    precondition, failure, success = _get_verification()
                    if precondition is KPValue.FALSE:
                        test_case_result = TestResult.Result.SKIPPED
                    elif failure is KPValue.TRUE:
                        test_case_result = TestResult.Result.FAILED
                    elif success is KPValue.TRUE:
                        test_case_result = TestResult.Result.SUCCEEDED
                    else:
                        # TODO Measure AI time start here?
                        self._request_control_avs(vids.vids)
                        cycle_end_time = datetime.now()
                        cycle_start_timestamp = Num()
                        cycle_start_timestamp.num = int(datetime.timestamp(cycle_start_time))
                        cycle_end_timestamp = Num()
                        cycle_end_timestamp.num = int(datetime.timestamp(cycle_end_time))
                        self.send_message_to_sim_node(b"storeVerificationCycle", [
                            self.serialized_sid,
                            cycle_start_timestamp.SerializeToString(),
                            cycle_end_timestamp.SerializeToString()
                        ])
                        self.send_message_to_sim_node(b"steps", [self.serialized_sid, serialized_frequency])
                else:
                    break
            result_queue.put(test_case_result)

        # FIXME Wait for simulation to be registered at the simulation node?
        # FIXME Use is_simulation_running?
        response = self.send_message_to_sim_node(b"vids", [self.serialized_sid])
        vids = VehicleIDs()
        vids.ParseFromString(response)
        freq = Num()
        freq.num = ai_frequency
        serialized_frequency = freq.SerializeToString()
        # print(self.sid.sid + ": vids: " + str(vids.vids))
        result_queue = Queue()
        cycles_thread = Thread(target=_run_verification_cycles, args=(result_queue,))
        test_start_time = datetime.now()
        cycles_thread.start()
        cycles_thread.join(TIMEOUT)
        result = TestResult()
        result.result = result_queue.get()
        self.send_message_to_sim_node(b"stop", [self.serialized_sid, result.SerializeToString()])

    @static_vars(port=60000, lock=Lock())
    def _start_simulation(self, test_case: TestCase) -> Tuple[Scenario, ExtThread]:
        from threading import Thread
        from config import BEAMNG_LEVEL_NAME
        from beamngpy.beamngcommon import BNGValueError

        Simulation._start_simulation.lock.acquire()
        while not Simulation._is_port_available(Simulation._start_simulation.port):
            Simulation._start_simulation.port += 200  # Make sure to not interfere with previously started simulations
        self._bng_instance = DBBeamNGpy('localhost', Simulation._start_simulation.port)
        authors = ", ".join(test_case.authors)
        bng_scenario = Scenario(BEAMNG_LEVEL_NAME, self._sim_name, authors=authors)

        test_case.scenario.add_all(bng_scenario)
        bng_scenario.make(self._bng_instance)

        # Make manual changes to the scenario files
        self._make_lanes_visible()
        self._annotate_objects()
        self._add_waypoints_to_scenario(test_case.scenario.participants)
        self._enable_participant_movements(test_case.scenario.participants)
        waypoints = set()
        for wps in [p.movement for p in test_case.scenario.participants]:
            for wp in wps:
                if wp.id is not None:  # FIXME Waypoints are added in wrong order
                    waypoints.add(wp.id)
        # self._add_lap_config(waypoints)

        try:
            self._bng_instance.open(launch=True)
            self._bng_instance.load_scenario(bng_scenario)
            self._bng_instance.set_steps_per_second(test_case.stepsPerSecond)
            self._bng_instance.set_deterministic()
            test_case.scenario.set_time_of_day_to(self._bng_instance)
            self._bng_instance.hide_hud()
            self._bng_instance.start_scenario()
            self._bng_instance.pause()
        except OSError:
            _logger.exception(
                "The start of a BeamNG instance failed (Port: " + str(Simulation._start_simulation.port) + ").")
        except BNGValueError:
            _logger.exception("Sending to or receiving from BeamNGpy failed and may corrupt the socket")
        Simulation._start_simulation.lock.release()

        runtime_thread = Thread(target=Simulation._run_runtime_verification, args=(self, test_case.aiFrequency))
        runtime_thread.daemon = True
        runtime_thread.start()

        while not runtime_thread.ident:  # Wait for the Thread to get an ID
            pass
        return bng_scenario, ExtThread(runtime_thread.ident)


@static_vars(counter=0)
def run_test_case(test_case: TestCase) -> Tuple[Simulation, Scenario, ExtThread, SimulationID]:
    """
    This method starts the actual simulation in a separate thread.
    Additionally it already calculates and attaches all information that is need by this node and the separate
    thread before calling _start_simulation(...).
    """
    import dill as pickle
    from drivebuildclient import create_client, send_request
    from config import SIM_NODE_PORT, FIRST_SIM_PORT
    sid = SimulationID()
    response = send_request(create_client("localhost", SIM_NODE_PORT), b"generateSid", [])
    sid.ParseFromString(response)
    sim = Simulation(sid, pickle.dumps(test_case), FIRST_SIM_PORT + run_test_case.counter)
    run_test_case.counter += 1  # FIXME Add a lock?
    # Make sure there is no folder of previous tests having the same sid that got not propery removed
    bng_scenario, thread = sim._start_simulation(test_case)

    return sim, bng_scenario, thread, sid
