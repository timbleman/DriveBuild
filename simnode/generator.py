from logging import getLogger
from typing import List, Tuple, Optional

from beamngpy import BeamNGpy
from drivebuildclient import static_vars
from lxml.etree import _ElementTree, _Element

_logger = getLogger("DriveBuild.SimNode.Generator")


class ScenarioBuilder:
    from beamngpy import Scenario
    from dbtypes.scheme import Road, Obstacle, Participant

    def __init__(self, lanes: List[Road], obstacles: List[Obstacle], participants: List[Participant],
                 time_of_day: Optional[float]):
        if participants is None:
            participants = list()
        self.roads = lanes
        self.obstacles = obstacles
        self.participants = participants
        self.time_of_day = time_of_day

    @static_vars(line_width=0.15, num_nodes=100, smoothness=0, markings_smoothing=0.13)
    def add_roads_to_scenario(self, scenario: Scenario) -> None:
        from beamngpy import Road
        from shapely.geometry import LineString
        from scipy.interpolate import splev, splprep
        from numpy.ma import arange
        from numpy import repeat, linspace
        from collections import defaultdict

        @static_vars(rounding_precision=3)
        def _interpolate_nodes(old_x_vals: List[float], old_y_vals: List[float], old_width_vals: List[float],
                               num_nodes: int) -> Tuple[List[float], List[float], List[float], List[float]]:
            assert len(old_x_vals) == len(old_y_vals) == len(old_width_vals), \
                "The lists for the interpolation must have the same length."
            k = 1 if len(old_x_vals) <= 3 else 3
            pos_tck, pos_u = splprep([old_x_vals, old_y_vals], s=self.add_roads_to_scenario.smoothness, k=k)
            step_size = 1 / num_nodes
            unew = arange(0, 1 + step_size, step_size)
            new_x_vals, new_y_vals = splev(unew, pos_tck)
            z_vals = repeat(0.01, len(unew))
            width_tck, width_u = splprep([pos_u, old_width_vals], s=self.add_roads_to_scenario.smoothness, k=k)
            _, new_width_vals = splev(unew, width_tck)
            # Reduce floating point rounding errors otherwise these may cause problems with calculating parallel_offset
            return [round(v, _interpolate_nodes.rounding_precision) for v in new_x_vals], \
                   [round(v, _interpolate_nodes.rounding_precision) for v in new_y_vals], \
                   z_vals, new_width_vals

        for road in self.roads:
            unique_nodes = []
            node_pos_tracker = defaultdict(lambda: list())
            for node in road.nodes:
                x = node.position[0]
                y = node.position[1]
                if x not in node_pos_tracker or y not in node_pos_tracker[x]:
                    unique_nodes.append(node)
                    node_pos_tracker[x].append(y)
            old_x_vals = [node.position[0] for node in unique_nodes]
            old_y_vals = [node.position[1] for node in unique_nodes]
            old_width_vals = [node.width for node in unique_nodes]
            # FIXME Set interpolate=False for all roads?
            main_road = Road('road_rubber_sticky', rid=road.rid)
            new_x_vals, new_y_vals, z_vals, new_width_vals \
                = _interpolate_nodes(old_x_vals, old_y_vals, old_width_vals, self.add_roads_to_scenario.num_nodes)
            main_nodes = list(zip(new_x_vals, new_y_vals, z_vals, new_width_vals))
            main_road.nodes.extend(main_nodes)
            scenario.add_road(main_road)
            # FIXME Recognize changing widths --- Basic drawing works for all the roads I have testes so far,
            #  however strong width changes cause stair stepping, this can be countered by a smoothing parameter,
            #  but this itself can introduce low poly lines and inaccuracies. Better post processing or a dynamic
            #  sampling rate may fix this.
            road_width = unique_nodes[0].width
            if road.markings:
                def _calculate_parallel_coords(offset: float, line_width: float) \
                        -> Optional[List[Tuple[float, float, float, float]]]:
                    original_line = LineString(zip(new_x_vals, new_y_vals))
                    try:
                        offset_line = original_line.parallel_offset(offset)
                        coords = offset_line.coords.xy
                    except (NotImplementedError, Exception):  # FIXME Where is TopologyException
                        _logger.exception("Creating an offset line for lane markings failed")
                        return None
                    # NOTE The parallel LineString may have a different number of points than initially given
                    num_coords = len(coords[0])
                    z_vals = repeat(0.01, num_coords)
                    marking_widths = repeat(line_width, num_coords)
                    return list(zip(coords[0], coords[1], z_vals, marking_widths))

                def _calculate_offset_list(relative_offset: float, absolute_offset: float,
                                           output_number_of_points: int = self.add_roads_to_scenario.num_nodes // 3) \
                        -> List[float]:
                    """ calculates a list of relative offsets to the road centre
                    uses new_width_vals for dynamic offset
                    change the default value of output_number_of_points for more precision, has to be less \
                    than num_nodes//2

                    :param relative_offset: relative to the width of the road, must be between -0.5 and 0.5
                    :param absolute_offset: absolute, to account for line width
                    :param output_number_of_points: number of outputs in list
                    :return: list of width offsets
                    """
                    assert 0 < output_number_of_points < new_width_vals.__len__() // 2, \
                        "choose a valid number of output vals"
                    assert -0.5 <= relative_offset <= 0.5, "relative offset must be between -0.5 and 0.5"
                    assert -max(new_width_vals) / 2 < absolute_offset < max(new_width_vals) / 2, \
                        "absolute offset must be smaller than half of the road"

                    cutting_points = linspace(0, new_width_vals.__len__() - 1, dtype=int, num=output_number_of_points)
                    output_vals = list(
                        map(lambda i: new_width_vals[i] * relative_offset + absolute_offset, cutting_points))
                    return output_vals

                def _calculate_parallel_pieces(offset: List[float], cutting_points: List[int]) \
                        -> Tuple[List[float], List[float]]:
                    """ This method will calculate offsets for smaller pieces of road.
                    uses new_x_vals and new_y_vals as baseline road

                    :param offset: list of width offsets, must be smaller than num_nodes//2, should be equidistant
                    :param cutting_points: list of points at which the road is split into pieces
                    :return: Returns a tuple of float lists for x and y values
                    """
                    assert cutting_points.__len__() < self.add_roads_to_scenario.num_nodes // 2, \
                        "too many cutting points"
                    assert new_x_vals.__len__() > 1 and new_y_vals.__len__() > 1 and new_width_vals.__len__() > 1, \
                        "cannot work with an empty road or a point"

                    original_line = LineString(zip(new_x_vals, new_y_vals))

                    i = 0
                    previous_p = 0
                    offset_sub_lines_x = []
                    offset_sub_lines_y = []
                    for p in cutting_points:
                        if p > previous_p:
                            new_x_piece, new_y_piece = _road_piece(offset[i], original_line,
                                                                   first_cutting_point=previous_p,
                                                                   second_cutting_point=p)
                            offset_sub_lines_x.extend(new_x_piece)
                            offset_sub_lines_y.extend(new_y_piece)
                        previous_p = p
                        i += 1
                    return offset_sub_lines_x, offset_sub_lines_y

                def _road_piece(offset: float, original_line: LineString, first_cutting_point: int,
                                second_cutting_point: int) \
                        -> Tuple[List[float], List[float]]:
                    """ helper method for _calculate_parallel_pieces, calculates each road piece for a certain offset

                    :param offset: absolute offset of the piece
                    :param original_line: LineString of baseline road coordinates
                    :param first_cutting_point: first point to split
                    :param second_cutting_point: second point to split
                    :return: returns a tuple of float lists for x and y values
                    """
                    from shapely.errors import TopologicalError
                    try:
                        piece_coords = original_line.coords[first_cutting_point: second_cutting_point]
                        road_lnstr = LineString(piece_coords).parallel_offset(offset)
                        offset_sub_lines = road_lnstr.coords.xy
                        # shapely reverses if the offset is positive
                        if offset > 0:
                            offset_sub_lines[0].reverse()
                            offset_sub_lines[1].reverse()
                        return offset_sub_lines
                    except ValueError:
                        _logger.exception("Some portions of the LineString are empty")
                    except TopologicalError:
                        _logger.exception("Shapely cannot create a valid polygon")

                def _smoothen_line(offset_sub_lines_x: List[float], offset_sub_lines_y: List[float]) \
                        -> Tuple[List[float], List[float]]:
                    """ Smoothens a line by the usage of LineString.simplify() and reduces stair stepping

                    :param offset_sub_lines: Tuple of float lists for x and y values
                    :return: Smoothed tuple of float lists for x and y values
                    """
                    assert offset_sub_lines_x.__len__() > 1 and offset_sub_lines_y.__len__() > 1, \
                        "cannot smooth an empty line or a point"

                    point_list = list(map(lambda i: (offset_sub_lines_x[i], offset_sub_lines_y[i]),
                                          range(0, offset_sub_lines_x.__len__() - 1)))
                    lstr = LineString(point_list)
                    lstr = lstr.simplify(tolerance=self.add_roads_to_scenario.markings_smoothing)
                    return lstr.coords.xy

                def _calculate_parallel_coords_list(offset: List[float], line_width: float) \
                        -> Optional[List[Tuple[float, float, float, float]]]:
                    """ calculates parallel coordinates of a road

                    :param offset: list of offsets, must be smaller than num_nodes//2, should be equidistant
                    :param line_width: specifies the width of the output coordinates
                    :return: coordinates for the road
                    """

                    assert offset.__len__() < self.add_roads_to_scenario.num_nodes // 2, \
                        "there have to be less than half offset points of num_node for shapely LineStrings to work"

                    num_points = self.add_roads_to_scenario.num_nodes
                    # cutting points for LineString
                    cutting_points = linspace(0, num_points - 1, dtype=int,
                                              num=min(num_points, offset.__len__())).tolist()
                    # extend the last point just a bit to get all nodes
                    cutting_points[-1] = cutting_points[-1] + cutting_points[-1] - cutting_points[-2]

                    offset_sub_lines_x, offset_sub_lines_y = _calculate_parallel_pieces(offset, cutting_points)
                    coords = _smoothen_line(offset_sub_lines_x, offset_sub_lines_y)
                    # NOTE The parallel LineString may have a different number of points than initially given
                    num_coords = len(coords[0])
                    z_vals = repeat(0.01, num_coords)
                    marking_widths = repeat(line_width, num_coords)
                    return list(zip(coords[0], coords[1], z_vals, marking_widths))

                # Draw side lines
                side_line_offset = 1.5 * self.add_roads_to_scenario.line_width
                left_side_line = Road('line_white', rid=road.rid + "_left_line")
                offset_list_line_left = _calculate_offset_list(relative_offset=-0.5,
                                                               absolute_offset=side_line_offset * 1.5)
                left_side_line_nodes = _calculate_parallel_coords_list(offset_list_line_left,
                                                                       self.add_roads_to_scenario.line_width)
                if left_side_line_nodes:
                    left_side_line.nodes.extend(left_side_line_nodes)
                    scenario.add_road(left_side_line)
                else:
                    _logger.warning("Could not create left side line")
                right_side_line = Road('line_white', rid=road.rid + "_right_line")
                offset_list_line_right = _calculate_offset_list(relative_offset=.5, absolute_offset=-side_line_offset)
                right_side_line_nodes = _calculate_parallel_coords_list(offset_list_line_right,
                                                                        self.add_roads_to_scenario.line_width)
                if right_side_line_nodes:
                    right_side_line.nodes.extend(right_side_line_nodes)
                    scenario.add_road(right_side_line)
                else:
                    _logger.warning("Could not create right side line")

                # Draw line separating left from right lanes
                if road.left_lanes > 0 and road.right_lanes > 0:
                    divider_rel_off = -0.5 + road.left_lanes / (road.left_lanes + road.right_lanes)
                    offset_list_divider = _calculate_offset_list(relative_offset=divider_rel_off,
                                                                 absolute_offset=-side_line_offset)
                    left_right_divider = Road("line_yellow_double", rid=road.rid + "_left_right_divider")
                    left_right_divider_nodes \
                        = _calculate_parallel_coords_list(offset_list_divider,
                                                          2 * self.add_roads_to_scenario.line_width)
                    if left_right_divider_nodes:
                        left_right_divider.nodes.extend(left_right_divider_nodes)
                        scenario.add_road(left_right_divider)
                    else:
                        _logger.warning("Could not create line separating lanes having different directions")

                # Draw lines separating left and right lanes from each other
                total_num_lane_markings = road.left_lanes + road.right_lanes
                offsets_dashed = linspace(-0.5, 0.5, num=total_num_lane_markings, endpoint=False).tolist()
                offsets_dashed = offsets_dashed[1:len(offsets_dashed)]
                # do not draw dashed line over divider line
                if road.left_lanes > 0 and road.right_lanes > 0:
                    offsets_dashed.remove(offsets_dashed[road.left_lanes - 1])
                # add each separating line
                for offs in offsets_dashed:
                    # '.' have to be removed from the name, else there are prefab parsing errors for positive offsets
                    lane_separation_line = Road('line_dashed_short',
                                                rid=road.rid + "_separator_" + str(offs).replace('.', ''))
                    offs_list = _calculate_offset_list(offs, 0)
                    lane_separation_line_nodes \
                        = _calculate_parallel_coords_list(offs_list, self.add_roads_to_scenario.line_width)

                    if lane_separation_line_nodes:
                        lane_separation_line.nodes.extend(lane_separation_line_nodes)
                        scenario.add_road(lane_separation_line)
                    else:
                        _logger.warning("Could not create line separating lanes having the same direction")

    def add_obstacles_to_scenario(self, scenario: Scenario) -> None:
        from beamngpy import ProceduralCone, ProceduralCube, ProceduralCylinder, ProceduralBump, StaticObject
        from dbtypes.scheme import Cone, Cube, Cylinder, Bump, Stopsign, TrafficLightSingle, TrafficLightDouble
        from random import randrange
        for obstacle in self.obstacles:
            obstacle_type = type(obstacle)
            height = obstacle.height
            pos = (obstacle.x, obstacle.y, height / 2.0)
            rot = (obstacle.x_rot, obstacle.y_rot, obstacle.z_rot)
            name = obstacle.oid
            mesh = None
            if obstacle_type is Cube:
                mesh = ProceduralCube(pos, rot, (obstacle.length, obstacle.width, height), name=name)
            elif obstacle_type is Cylinder:
                mesh = ProceduralCylinder(pos, rot, obstacle.radius, height=height, name=name)
            elif obstacle_type is Cone:
                mesh = ProceduralCone(pos, rot, obstacle.base_radius, height, name=name)
            elif obstacle_type is Bump:
                mesh = ProceduralBump(pos, rot, obstacle.width, obstacle.length, height, obstacle.upper_length,
                                      obstacle.upper_width, name=name)
            elif obstacle_type is Stopsign:
                id_number = randrange(1000)
                name_sign = "stopsign" + str(id_number)
                stopsign = StaticObject(pos=(obstacle.x, obstacle.y, 0), rot=rot, name=name_sign, scale=(3, 3, 3),
                                        shape='/levels/drivebuild/art/objects/stopsign.dae')
                scenario.add_object(stopsign)
            elif obstacle_type is TrafficLightSingle:
                id_number = randrange(1000)
                name_light = "trafficlight" + str(id_number)
                name_pole = "pole" + str(id_number)
                traffic_light = StaticObject(name=name_light, pos=(obstacle.x, obstacle.y, 5.32), rot=rot,
                                             scale=(1, 1, 1),
                                             shape='/levels/drivebuild/art/objects/trafficlight1a.dae')
                scenario.add_object(traffic_light)
                pole = StaticObject(name=name_pole, pos=(obstacle.x, obstacle.y, 0), rot=rot, scale=(1, 1, 1.3),
                                    shape='/levels/drivebuild/art/objects/pole_traffic1.dae')
                scenario.add_object(pole)
            elif obstacle_type is TrafficLightDouble:
                from math import radians, sin, cos
                from numpy import dot
                id_number = randrange(1000)
                name_light1 = "trafficlight" + str(id_number)
                name_light2 = "trafficlight" + str(id_number) + 'a'
                name_pole = "pole" + str(id_number)
                rad = radians(obstacle.z_rot)
                pole_coords = (obstacle.x, obstacle.y, 0)
                traffic_light1_coords = (7.5, 0.35)
                traffic_light2_coords = (3, 0.35)
                rot_matrix = [[cos(rad), sin(rad)], [-sin(rad), cos(rad)]]
                traffic_light1_coords = dot(rot_matrix, traffic_light1_coords)
                traffic_light1_coords = (
                    traffic_light1_coords[0] + pole_coords[0], traffic_light1_coords[1] + pole_coords[1], 7.8)
                traffic_light2_coords = dot(rot_matrix, traffic_light2_coords)
                traffic_light2_coords = (
                    traffic_light2_coords[0] + pole_coords[0], traffic_light2_coords[1] + pole_coords[1], 7.3)

                pole2 = StaticObject(name=name_pole, pos=pole_coords, rot=rot, scale=(1, 1, 1),
                                     shape='/levels/drivebuild/art/objects/pole_light_signal1.dae')
                scenario.add_object(pole2)
                traffic_light1 = StaticObject(name=name_light1, pos=traffic_light1_coords, rot=rot, scale=(1, 1, 1),
                                              shape='/levels/drivebuild/art/objects/trafficlight2a.dae')
                scenario.add_object(traffic_light1)
                traffic_lights2 = StaticObject(name=name_light2, pos=traffic_light2_coords, rot=rot, scale=(1, 1, 1),
                                               shape='/levels/drivebuild/art/objects/trafficlight2a.dae')
                scenario.add_object(traffic_lights2)
            else:
                _logger.warning(
                    "Obstacles of type " + str(obstacle_type) + " are not supported by the generation, yet.")
                mesh = None
            if mesh:
                # NOTE Procedural meshes use radians for rotation
                scenario.add_procedural_mesh(mesh)

    def add_participants_to_scenario(self, scenario: Scenario) -> None:
        from dbtypes.beamng import DBVehicle
        for participant in self.participants:
            # FIXME Adjust color
            vehicle = DBVehicle(participant.id, model=participant.model, color="White", licence=participant.id)
            for request in participant.ai_requests:
                vehicle.apply_request(request)
            initial_state = participant.initial_state
            # NOTE Participants use degrees for rotation
            scenario.add_vehicle(vehicle,
                                 pos=(initial_state.position[0], initial_state.position[1], 0),
                                 rot=(0, 0, -initial_state.orientation - 90))

    def add_waypoints_to_scenario(self, scenario: Scenario) -> None:
        """
        As long as manually inserting text the temporary method sim_controller.py::add_waypoints_to_scenario has to be
        used.
        used.
        """
        pass

    def set_time_of_day_to(self, instance: BeamNGpy):
        if self.time_of_day:
            instance.set_tod(self.time_of_day)

    def add_all(self, scenario: Scenario) -> None:
        # NOTE time_of_day has to be called on the BeamNG instance not on a scenario
        self.add_roads_to_scenario(scenario)
        self.add_obstacles_to_scenario(scenario)
        self.add_participants_to_scenario(scenario)
        self.add_waypoints_to_scenario(scenario)


def generate_scenario(env: _ElementTree, participants_node: _Element) -> ScenarioBuilder:
    from lxml.etree import _Element
    from dbtypes.scheme import RoadNode, Road, Participant, InitialState, MovementMode, CarModel, WayPoint, Cube, \
        Cylinder, Cone, Bump, Stopsign, TrafficLightSingle, TrafficLightDouble
    from util.xml import xpath, get_tag_name
    from requests import PositionRequest, SpeedRequest, SteeringAngleRequest, CameraRequest, CameraDirection, \
        LidarRequest, RoadCenterDistanceRequest, CarToLaneAngleRequest, BoundingBoxRequest, RoadEdgesRequest

    roads: List[Road] = list()

    @static_vars(prefix="road_", counter=0)
    def _generate_road_id() -> str:
        while True:  # Pseudo "do-while"-loop
            rid = _generate_road_id.prefix + str(_generate_road_id.counter)
            if rid in map(lambda l: l.rid, roads):
                _generate_road_id.counter += 1
            else:
                break
        return rid

    road_nodes = xpath(env, "db:lanes/db:lane")
    for node in road_nodes:
        road_segment_nodes = xpath(node, "db:laneSegment")
        road = Road(list(
            map(
                lambda n: RoadNode((float(n.get("x")), float(n.get("y"))), float(n.get("width"))),
                road_segment_nodes
            )
        ), node.get("markings", "true").lower() == "true",
            int(node.get("leftLanes", "0")),
            int(node.get("rightLanes", "1")),
            node.get("id", _generate_road_id()))
        roads.append(road)

    def get_obstacle_common(node: _Element) -> Tuple[float, float, float, float, float, float, Optional[str]]:
        """
        Returns the attributes all types of obstacles have in common.
        :param node: The obstacle node
        :return: x, y, x_rot, y_rot, z_rot, height, id
        """
        return float(node.get("x")), float(node.get("y")), float(node.get("xRot", 0)), float(node.get("yRot", 0)), \
               float(node.get("zRot", 0)), float(node.get("height")), node.get("id", None)

    obstacles = list()
    cube_nodes = xpath(env, "db:obstacles/db:cube")
    for node in cube_nodes:
        x, y, x_rot, y_rot, z_rot, height, oid = get_obstacle_common(node)
        width = float(node.get("width"))
        length = float(node.get("length"))
        obstacles.append(Cube(x, y, height, width, length, oid, x_rot, y_rot, z_rot))

    cylinder_nodes = xpath(env, "db:obstacles/db:cylinder")
    for node in cylinder_nodes:
        x, y, x_rot, y_rot, z_rot, height, oid = get_obstacle_common(node)
        radius = float(node.get("radius"))
        obstacles.append(Cylinder(x, y, height, radius, oid, x_rot, y_rot, z_rot))

    cone_nodes = xpath(env, "db:obstacles/db:cone")
    for node in cone_nodes:
        x, y, x_rot, y_rot, z_rot, height, oid = get_obstacle_common(node)
        base_radius = float(node.get("baseRadius"))
        obstacles.append(Cone(x, y, height, base_radius, oid, x_rot, y_rot, z_rot))

    stopsign_nodes = xpath(env, "db:obstacles/db:stopsign")
    for node in stopsign_nodes:
        x, y, x_rot, y_rot, z_rot, height, oid = get_obstacle_common(node)
        obstacles.append(Stopsign(x, y, height, oid, x_rot, y_rot, z_rot))

    traffic_light_single_nodes = xpath(env, "db:obstacles/db:trafficlightsingle")
    for node in traffic_light_single_nodes:
        x, y, x_rot, y_rot, z_rot, height, oid = get_obstacle_common(node)
        obstacles.append(TrafficLightSingle(x, y, height, oid, x_rot, y_rot, z_rot))

    traffic_light_double_nodes = xpath(env, "db:obstacles/db:trafficlightdouble")
    for node in traffic_light_double_nodes:
        x, y, x_rot, y_rot, z_rot, height, oid = get_obstacle_common(node)
        obstacles.append(TrafficLightDouble(x, y, height, oid, x_rot, y_rot, z_rot))

    bump_nodes = xpath(env, "db:obstacles/db:bump")
    for node in bump_nodes:
        x, y, x_rot, y_rot, z_rot, height, oid = get_obstacle_common(node)
        length = float(node.get("length"))
        width = float(node.get("width"))
        upper_length = float(node.get("upperLength"))
        upper_width = float(node.get("upperWidth"))
        obstacles.append(Bump(x, y, height, width, length, upper_length, upper_width, oid, x_rot, y_rot, z_rot))

    def _extract_common_state_vals(n: _Element) -> Tuple[MovementMode, Optional[float], Optional[float]]:
        speed_limit = n.get("speedLimit")
        target_speed = n.get("speed")
        return MovementMode[n.get("movementMode")], \
               None if speed_limit is None else float(speed_limit) / 3.6, \
               None if target_speed is None else float(target_speed) / 3.6

    participants = list()
    participant_nodes = xpath(participants_node, "db:participant")
    for node in participant_nodes:
        pid = node.get("id")
        initial_state_node = xpath(node, "db:initialState")[0]
        common_state_vals = _extract_common_state_vals(initial_state_node)
        initial_state = InitialState(
            (float(initial_state_node.get("x")), float(initial_state_node.get("y"))),
            float(initial_state_node.get("orientation")),
            common_state_vals[0],
            common_state_vals[1],
            common_state_vals[2]
        )
        # Add data requests declared in the DBC
        ai_requests = list()
        request_nodes = xpath(node, "db:ai/*")
        for req_node in request_nodes:
            tag = get_tag_name(req_node)
            rid = req_node.get("id")
            if tag == "position":
                ai_requests.append(PositionRequest(rid))
            elif tag == "speed":
                ai_requests.append(SpeedRequest(rid))
            elif tag == "steeringAngle":
                ai_requests.append(SteeringAngleRequest(rid))
            elif tag == "camera":
                width = int(req_node.get("width"))
                height = int(req_node.get("height"))
                fov = int(req_node.get("fov"))
                direction = CameraDirection[req_node.get("direction")]
                ai_requests.append(CameraRequest(rid, width, height, fov, direction))
            elif tag == "lidar":
                radius = int(req_node.get("radius"))
                ai_requests.append(LidarRequest(rid, radius))
            elif tag == "roadCenterDistance":
                ai_requests.append(RoadCenterDistanceRequest(rid, roads))
            elif tag == "carToLaneAngle":
                ai_requests.append(CarToLaneAngleRequest(rid, roads))
            elif tag == "boundingBox":
                ai_requests.append(BoundingBoxRequest(rid))
            elif tag == "roadEdges":
                ai_requests.append(RoadEdgesRequest(rid))
            else:
                _logger.warning("The tag " + tag + " is not supported, yet.")
        # Add default data requests required for debugging and visualization
        ai_requests.extend([
            BoundingBoxRequest("visualizer_" + pid + "_boundingBox")
        ])
        # Extract the movement of the participant
        movements = list()
        waypoint_nodes = xpath(node, "db:movement/db:waypoint")
        for wp_node in waypoint_nodes:
            common_state_vals = _extract_common_state_vals(wp_node)
            movements.append(WayPoint(
                (float(wp_node.get("x")), float(wp_node.get("y"))),
                float(wp_node.get("tolerance")),
                wp_node.get("id"),
                common_state_vals[0],
                common_state_vals[1],
                common_state_vals[2]
            ))
        participants.append(Participant(pid, initial_state, CarModel[node.get("model")].value, movements, ai_requests))

    time_of_day_elements = xpath(env, "db:timeOfDay")
    time_of_day = float(time_of_day_elements[0].text) if time_of_day_elements else None

    return ScenarioBuilder(roads, obstacles, participants, time_of_day)
