import bpy
import random
import math
import mathutils
from mathutils import Vector, Matrix, Quaternion, Euler
import uuid, time  # for id generation

# careful: make sure to apply transformations (especially scale) to curve !!!

# todo: currently, points_distance_percent along tangent doesn't consider scale (of both previous object and curr object)
# because scaling happens after we calc (extents - pivot)
# nor does it account for rotation (for the same reason. its after.)
# todo: normal extents doesn't account for pivot, like how old tangent extent was. needs fix
# edit: all is fixed :)


def pick_alternate(scatter_objs, instance_count):
    obj_idx = instance_count % len(scatter_objs)
    return scatter_objs[obj_idx]


def pick_random(scatter_objs, instance_count):
    return random.choice(scatter_objs)


def pick_shuffle_cycle(
    scatter_objs, instance_count
):  # Ensures all items are picked before repeating, but in a random order
    from itertools import cycle

    shuffled = random.sample(scatter_objs, len(scatter_objs))
    return cycle(shuffled)


# REQUIRES BLENDER 4.0
def scatter_objects_on_curve(
    objects=None,
    # tangent
    points_distance=1.0,
    random_points_distance=False,
    random_points_distance_range=Vector((1.0, 5.0)),
    points_distance_percent=0.0,  # percent of bbox added to pts_dist
    position_offset=Vector((0, 0, 0)),
    # scale
    random_scale=False,
    random_scale_min=Vector((0.2, 0.2, 0.2)),
    random_scale_max=Vector((1.2, 1.2, 1.2)),
    random_scale_uniform=True,
    random_scale_uniform_range=Vector((0.2, 1.2)),
    # rotation
    random_rot=False,
    random_rot_range=Vector((1.0, 1.0, 1.0)),
    random_flip=Vector((0, 0, 0)),
    align_to_tangent=0.0,  # strength -1..1 (float) - negative values flip direction
    align_to_normal=0.0,  # strength -1..1 (float) - negative values flip direction
    forward_axis="-Y",  # which object axis should point along curve: 'X', 'Y', or 'Z'
    up_axis="Z",  # which object axis should point up (normal): 'X', 'Y', or 'Z'. cannot be = forward axis
    # radians: rotation of normal around tangent
    # 0 angle = from curve point to z-up, by convention
    normal_angle=math.radians(90),
    normal_offset=0,
    normal_offset_percent=0.0,
    normal_offset_random=0,  # random offset in the normal direction
    # binormal (perpendicular on tangent and normale)
    binormal_offset=0,
    binormal_offset_percent=0.0,
    binormal_offset_random=0,
    # misc
    use_collections=False,  # instead of objects, scatter collection of selected objects
    is_multi_obj_scatter=True,
    copy_data=False,
    select_result_clones=True,  # if not, original curve and objects will be kept
    straighten=False,  # super useful if you wanna apply "curve" modifier afterwards instead of scattering.
    pick_func=pick_alternate,
):
    selected_objects = bpy.context.selected_objects if objects is None else objects
    if len(selected_objects) < 2:
        print("Please select a curve and at least one object.")
        return

    # Separate curve(s) from scatter sources
    curve = None
    scatter_objs = []
    for obj in selected_objects:
        if obj.type == "CURVE":
            if curve is None:
                curve = obj if not straighten else create_straight_poly_curve(obj)
            else:
                print("Multiple curves found, using the first.")
        else:
            scatter_objs.append(obj)

    if curve is None:
        print("No curve selected.")
        return
    if not scatter_objs:
        print("No scatter objects selected.")
        return

    # Validate axis parameters
    # Check for negative signs
    is_forward_neg = forward_axis.startswith("-")
    is_up_neg = up_axis.startswith("-")

    # Get pure base letters (X, Y, or Z)
    forward_base = forward_axis.replace("-", "").upper()
    up_base = up_axis.replace("-", "").upper()

    valid_axes = ["X", "Y", "Z"]
    if forward_base not in valid_axes or up_base not in valid_axes:
        print("Invalid axis selection.")
        return
        
    if forward_base == up_base:
        print(f"Forward and Up base axes cannot be the same ('{forward_base}')")
        return

    # Determine the third (side) axis base letter
    side_base = [ax for ax in valid_axes if ax not in [forward_base, up_base]][0]

    # Map base letters to matrix column indices (0=X, 1=Y, 2=Z)
    axis_to_index = {"X": 0, "Y": 1, "Z": 2}
    forward_idx = axis_to_index[forward_base]
    up_idx = axis_to_index[up_base]
    side_idx = axis_to_index[side_base]
    
    # Collection for scattered objects
    # we make each name unique because sometimes we do not want 2 consecutive scatters
    # in the same collection. (but for a same scatter with different objects, its ok)

    random_id = (
        hex(int(time.time()))[2:] + hex(random.randint(0, 0xFFFF))[2:]
    )  # uuid.uuid4().hex is too long
    scatter_collection_name = f"Scattered_on_{curve.name}_{random_id}"
    if scatter_collection_name in bpy.data.collections:
        scatter_collection = bpy.data.collections[scatter_collection_name]
    else:
        scatter_collection = bpy.data.collections.new(scatter_collection_name)
        bpy.context.scene.collection.children.link(scatter_collection)

    def get_bbox_world_corners(obj):
        local_corners = [Vector(corner) for corner in obj.bound_box]
        return [obj.matrix_world @ c for c in local_corners]

    def get_extent_along_axis(corners, axis):
        axis = axis.normalized()
        projections = [c.dot(axis) for c in corners]
        return max(projections) - min(projections)

    def get_extents_from_pivot(obj, forward_idx, align_to_tangent):
        """
        Calculate the distances from the object's pivot to its furthest points
        in both forward and backward directions along a specified axis.

        Returns:
            (forward_extent, backward_extent)
        """
        local_corners = [Vector(corner) for corner in obj.bound_box]

        # Create forward axis vector in local space
        forward_axis_vector = Vector((0.0, 0.0, 0.0))
        forward_axis_vector[forward_idx] = 1.0

        # Account for direction flip if align_to_tangent is negative
        if align_to_tangent < 0:
            forward_axis_vector = -forward_axis_vector

        # Project all corners onto the forward axis
        projections = [corner.dot(forward_axis_vector) for corner in local_corners]

        # Calculate forward and backward extents from pivot (origin)
        forward_extent = max(0, max(projections))
        backward_extent = max(0, -min(projections))

        return forward_extent, backward_extent

    # Convert curve to mesh for sampling
    curve.data.resolution_u = 64
    temp_mesh = curve.to_mesh()
    if not temp_mesh or not temp_mesh.edges:
        print("Curve conversion failed.")
        return

    # Compute curve edges info
    total_length = 0.0
    edge_lengths = []
    edge_vectors = []
    verts = temp_mesh.vertices
    for edge in temp_mesh.edges:
        v0 = verts[edge.vertices[0]].co
        v1 = verts[edge.vertices[1]].co
        edge_vec = v1 - v0
        length = edge_vec.length
        total_length += length
        edge_lengths.append(length)
        # store normalized direction in local curve space
        edge_vectors.append(edge_vec.normalized() if length > 0 else Vector((0, 0, 1)))

    # world matrix for curve (transform sample to world space)
    curve_world_mat = curve.matrix_world.copy()
    curve_world_rot = curve_world_mat.to_3x3()

    # simple guard to avoid infinite loop when spacing becomes zero
    def safe_spacing(val):
        return val if val > 1e-6 else 1e-3

    created_clones = []

    # Scatter along curve
    # FIXED: Track the position where the last object's forward extent ends
    # This is where the next object should be placed (accounting for its backward extent)
    curve_position = (
        0.0  # Position along curve where we'll place the next object's pivot
    )
    instance_count = 0

    MAX_STACK_COUNT = 10000  # to prevent memory leak + blender crash when user puts a very low pts distance (or 0)
    curr_stack_count = 0
    while curve_position < total_length:
        curr_stack_count += 1
        if curr_stack_count >= MAX_STACK_COUNT:
            raise ValueError(
                "Stack overflow. you have provided a very low points distance"
            )

        # Pick which object to scatter (alternate)
        # obj_idx = instance_count % len(scatter_objs)
        # current_obj = scatter_objs[obj_idx]
        # we use custom scatter func instead.
        current_obj = pick_func(scatter_objs, instance_count)

        # Find the edge and position for placement
        walked = 0.0
        placed = False

        for i, edge in enumerate(temp_mesh.edges):
            v0 = verts[edge.vertices[0]].co
            v1 = verts[edge.vertices[1]].co
            edge_len = edge_lengths[i]

            tangent_local = edge_vectors[i].copy()
            world_tangent = (curve_world_rot @ tangent_local).normalized()

            # Compute a stable reference up vector (world Z by default)
            ref_up = Vector((0.0, 0.0, 1.0))
            # If tangent nearly parallel to ref_up, choose a different reference
            # do not do = 1, float precision can fuck you
            if abs(world_tangent.dot(ref_up)) > 0.999:
                ref_up = Vector((1.0, 0.0, 0.0))

            # Project ref_up onto plane perpendicular to tangent to obtain a normal candidate
            proj = ref_up - world_tangent * (ref_up.dot(world_tangent))
            if proj.length_squared == 0.0:
                # fallback if projection collapsed
                proj = Vector((1.0, 0.0, 0.0))
            normal_world = proj.normalized()

            # Binormal (side vector) from tangent x normal (ensures orthogonality)
            binormal_world = world_tangent.cross(normal_world).normalized()

            # Apply normal_angle rotation around tangent axis BEFORE building the rotation matrix
            if abs(normal_angle) > 1e-8:
                q = Quaternion(world_tangent, normal_angle)
                normal_world = (q @ normal_world).normalized()
                binormal_world = (q @ binormal_world).normalized()

            # Apply direction flips based on negative strengths
            tangent_dir = world_tangent.copy()
            normal_dir = normal_world.copy()
            binormal_dir = binormal_world.copy()

            if align_to_tangent < 0:
                tangent_dir = -tangent_dir
            if align_to_normal < 0:
                normal_dir = -normal_dir

            # Build target rotation matrix from basis vectors
            # Create rotation matrix based on user-specified axes
            target_matrix = Matrix.Identity(3)
            # Assign forward and up vectors, flipping them if a negative axis was chosen
            target_matrix.col[forward_idx] = -tangent_dir if is_forward_neg else tangent_dir
            target_matrix.col[up_idx] = -normal_dir if is_up_neg else normal_dir
            # The side axis vector is calculated dynamically to stay right-handed
            target_matrix.col[side_idx] = binormal_dir

            # Convert to quaternion
            target_quat = target_matrix.to_quaternion()

            # Store the object's original rotation (before alignment)
            original_quat = (
                current_obj.rotation_quaternion.copy()
                if current_obj.rotation_mode == "QUATERNION"
                else current_obj.rotation_euler.to_quaternion()
            )

            # Apply alignment strengths (now supporting -1 to 1 range)
            final_quat = original_quat.copy()

            # Apply tangent alignment
            tangent_strength = abs(align_to_tangent)
            if tangent_strength > 1e-6:
                tangent_strength = max(0.0, min(1.0, tangent_strength))
                final_quat = final_quat.slerp(target_quat, tangent_strength)

            # Apply normal alignment
            normal_strength = abs(align_to_normal)
            if normal_strength > 1e-6:
                normal_strength = max(0.0, min(1.0, normal_strength))
                final_quat = final_quat.slerp(target_quat, normal_strength)

            # Random rotation applied in local object space (after alignment)
            if random_rot:
                # create random Euler rotation and apply it as additional rotation
                rx = random.uniform(-math.pi, math.pi) * random_rot_range.x
                ry = random.uniform(-math.pi, math.pi) * random_rot_range.y
                rz = random.uniform(-math.pi, math.pi) * random_rot_range.z
                rand_euler = Euler((rx, ry, rz))
                rand_q = rand_euler.to_quaternion()
                final_quat = rand_q @ final_quat

            flip_x = random.choice([0, math.pi]) if random_flip.x != 0 else 0
            flip_y = random.choice([0, math.pi]) if random_flip.y != 0 else 0
            flip_z = random.choice([0, math.pi]) if random_flip.z != 0 else 0

            if flip_x or flip_y or flip_z:
                flip_euler = Euler((flip_x, flip_y, flip_z))
                flip_q = flip_euler.to_quaternion()
                final_quat = flip_q @ final_quat

            scale = (1, 1, 1)
            # Random scale
            if random_scale:
                if random_scale_uniform:
                    f = random.uniform(
                        random_scale_uniform_range.x, random_scale_uniform_range.y
                    )
                    scale = (f, f, f)
                elif random_scale:
                    scale = (
                        random.uniform(random_scale_min.x, random_scale_max.x),
                        random.uniform(random_scale_min.y, random_scale_max.y),
                        random.uniform(random_scale_min.z, random_scale_max.z),
                    )

            old_rotation = current_obj.rotation_quaternion
            old_scale = current_obj.scale
            # apply transforms temporarily
            # current_obj.rotation_mode = "QUATERNION"
            # current_obj.rotation_quaternion = final_quat
            # current_obj.scale = scale

            def get_extents_from_pivot_world_space_along_tangent(
                obj, final_quat, scale, world_tangent, align_to_tangent
            ):
                """
                Calculate the distances from the object's pivot to its furthest points
                in both forward and backward directions along the curve's tangent direction,
                accounting for rotation and scale in world space.

                Returns:
                    (forward_extent, backward_extent)
                """
                # Get local bounding box corners
                local_corners = [Vector(corner) for corner in obj.bound_box]

                # Apply scale to corners
                scaled_corners = []
                for corner in local_corners:
                    scaled_corner = Vector(
                        (corner.x * scale[0], corner.y * scale[1], corner.z * scale[2])
                    )
                    scaled_corners.append(scaled_corner)

                # Apply rotation to scaled corners
                rotated_corners = []
                for corner in scaled_corners:
                    rotated_corner = final_quat @ corner
                    rotated_corners.append(rotated_corner)

                # Use the actual curve tangent direction (accounting for flip)
                tangent_direction = world_tangent.copy()
                if align_to_tangent < 0:
                    tangent_direction = -tangent_direction

                # Project all transformed corners onto the tangent direction
                projections = [
                    corner.dot(tangent_direction) for corner in rotated_corners
                ]

                # Calculate forward and backward extents from pivot (origin)
                forward_extent = max(0, max(projections))
                backward_extent = max(0, -min(projections))

                return forward_extent, backward_extent

            # Calculate current object's extents
            (
                current_forward_extent,
                current_backward_extent,
            ) = get_extents_from_pivot_world_space_along_tangent(
                current_obj, final_quat, scale, world_tangent, align_to_tangent
            )

            # restore
            # current_obj.rotation_quaternion = old_rotation
            # current_obj.scale = old_scale

            # FIXED: The actual placement position should account for the object's backward extent
            # so that the object's back edge aligns with the previous object's front edge
            placement_position = (
                curve_position + current_backward_extent * points_distance_percent
            )

            if walked + edge_len >= placement_position:
                # Found the edge where we should place the object
                t = (placement_position - walked) / edge_len if edge_len > 0 else 0.0
                loc_local = v0.lerp(v1, t)

                # get placement point, in world space
                world_loc = curve_world_mat @ loc_local

                # Create instance
                if use_collections:
                    inst = bpy.data.objects.new(f"CollInst_{instance_count}", None)
                    inst.instance_type = "COLLECTION"
                    inst.instance_collection = (
                        current_obj.users_collection[0]
                        if current_obj.users_collection
                        else None
                    )
                else:
                    # dont clone data
                    obj_data = current_obj.data
                    if copy_data:
                        obj_data = current_obj.data.copy()
                    inst = bpy.data.objects.new(
                        f"Scatter_{current_obj.name}_{instance_count}", obj_data
                    )

                scatter_collection.objects.link(inst)
                created_clones.append(inst)

                # Apply rotation
                inst.rotation_mode = "QUATERNION"
                inst.rotation_quaternion = final_quat

                # apply scale
                inst.scale = scale

                # Optional offset along normal
                (
                    current_normal_forward_extent,
                    current_normal_backward_extent,
                ) = get_extents_from_pivot_world_space_along_tangent(
                    current_obj, final_quat, scale, normal_world, align_to_tangent
                )
                nrand = random.uniform(-normal_offset_random, normal_offset_random)
                n_offset = normal_world * (
                    normal_offset
                    + nrand
                    + normal_offset_percent
                    * (
                        current_normal_forward_extent
                        if normal_offset_percent < 1
                        else current_normal_backward_extent
                    )
                )

                world_loc = world_loc + position_offset + n_offset
                inst.location = world_loc

                placed = True
                break

            walked += edge_len

        if not placed:
            break  # Couldn't place object, probably reached end of curve

        if random_points_distance:
            spacing = random.uniform(
                random_points_distance_range.x, random_points_distance_range.y
            )
        else:
            spacing = points_distance

        # FIXED: Update curve position for next object
        # Simple spacing: just add the object's forward extent and the desired spacing
        spacing = safe_spacing(spacing)

        # Move to the position where the next object should start (its backward edge)
        curve_position = (
            placement_position
            + current_forward_extent * points_distance_percent
            + spacing
        )

        instance_count += 1

    curve.to_mesh_clear()
    if select_result_clones and created_clones:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in created_clones:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = created_clones[0]

    print(
        f"Scattered {instance_count} instances of {len(scatter_objs)} objects on {curve.name}."
    )


def create_straight_poly_curve(curve, direction=Vector((0, -1, 0))):
    obj = bpy.context.active_object if curve is None else bpy.context.active_object
    # Check if it's a curve
    if not obj or obj.type != "CURVE":
        print("Please select a curve object")
        return None
    # Get the curve data
    curve = obj.data
    # Work with the first spline
    if not curve.splines:
        print("No splines found in curve")
        return None
    spline = curve.splines[0]
    # Calculate total length of the original curve
    total_length = 0.0
    points = []
    # Get points based on spline type
    if spline.type == "BEZIER":
        for i, point in enumerate(spline.bezier_points):
            # Transform point to world coordinates
            world_point = obj.matrix_world @ point.co
            points.append(world_point)
            if i > 0:
                total_length += (points[i] - points[i - 1]).length
    elif spline.type == "NURBS" or spline.type == "POLY":
        for i, point in enumerate(spline.points):
            # Transform point to world coordinates (convert from 4D to 3D)
            local_point = Vector(point.co[:3])
            world_point = obj.matrix_world @ local_point
            points.append(world_point)
            if i > 0:
                total_length += (points[i] - points[i - 1]).length
    if len(points) < 2:
        print("Need at least 2 points")
        return None
    # Create new curve data
    new_curve_data = bpy.data.curves.new(name=f"{obj.name}_straight", type="CURVE")
    new_curve_data.dimensions = "3D"
    # Create new spline
    new_spline = new_curve_data.splines.new(type="POLY")
    # Set number of points (subtract 1 because splines.new creates 1 point by default)
    num_points = len(points)
    new_spline.points.add(num_points - 1)

    # Normalize the direction vector
    direction_normalized = direction.normalized()

    # Create straight line points along the specified direction
    segment_length = total_length / (num_points - 1) if num_points > 1 else 0

    # Set the points for straight line along the direction vector starting at origin
    for i, point in enumerate(new_spline.points):
        new_pos = direction_normalized * (i * segment_length)
        # Poly curve points use 4D homogeneous coordinates
        point.co = (new_pos.x, new_pos.y, new_pos.z, 1.0)
    # Create new object
    new_obj = bpy.data.objects.new(f"{obj.name}_straight", new_curve_data)
    # Link to scene
    bpy.context.collection.objects.link(new_obj)
    # Select the new object
    # bpy.context.view_layer.objects.active = new_obj
    # new_obj.select_set(True)
    print(
        f"Created straight poly curve '{new_obj.name}' with length: {total_length:.3f}"
    )
    return new_obj


# Example usage (adjust args as needed)
# scatter_objects_on_curve(
#     points_distance=3,
#     points_distance_percent=0.0,
#     position_offset=Vector((0, 0, 0)),
#     random_scale_uniform=False,
#     random_rot=False,
#     random_rot_range=Vector((0, 0, 1)),
#     use_collections=False,
#     forward_axis="X",
#     align_to_tangent=1.0,
#     # align_to_normal=1,
#     # normal_angle=math.radians(45),  # radians; try math.radians(45) to tilt the normal
# )


bl_info = {
    "name": "Scatter Objects on Curve",
    "blender": (4, 0, 0),
    "category": "Object",
    "version": (1, 0, 0),
    "author": "aymen157",
    "location": "Object Menu > Scatter on curve",
    "description": "Scatter objects along a curve with randomization options",
    "doc_url": "https://github.com/aymen157/Blender-Scatter-On-Curve",
    "tracker_url": "https://github.com/aymen157/Blender-Scatter-On-Curve/issues",
}

import bpy
from bpy.types import Operator
from bpy.props import (
    FloatProperty,
    BoolProperty,
    EnumProperty,
    FloatVectorProperty,
    StringProperty,
)
import json
import math
from mathutils import Vector


# -----------------------------
# Preset Storage (Empty Object)
# -----------------------------


def get_preset_empty(create=False):
    """Get or create the preset storage empty"""
    preset_empty_name = "ScatterPresets_Data"

    if preset_empty_name in bpy.data.objects:
        return bpy.data.objects[preset_empty_name]

    if not create:
        return None

    empty = bpy.data.objects.new(preset_empty_name, None)
    empty.empty_display_type = "SPHERE"
    empty.empty_display_size = 0.1
    empty.hide_viewport = True
    empty.hide_render = True
    bpy.context.scene.collection.objects.link(empty)

    return empty


def save_preset(name, properties):
    """Save preset to empty object"""
    empty = get_preset_empty(create=True)
    empty[f"preset_{name}"] = json.dumps(properties)


def load_preset(name):
    """Load preset from empty object"""
    empty = get_preset_empty(create=False)
    if empty is None:
        return None
    preset_key = f"preset_{name}"
    return json.loads(empty[preset_key]) if preset_key in empty else None


def get_all_preset_names():
    """Get list of all preset names"""
    empty = get_preset_empty(create=False)
    if empty is None:
        return []
    return sorted(
        [
            key.replace("preset_", "")
            for key in empty.keys()
            if key.startswith("preset_")
        ]
    )


def delete_preset(name):
    """Delete a preset"""
    empty = get_preset_empty(create=False)
    if empty is None:
        return
    preset_key = f"preset_{name}"
    if preset_key in empty:
        del empty[preset_key]


def get_scatter_pick_items(self, context):
    return [(k, k, f"Use {k} picking method") for k in SCATTER_PICK_FUNCS.keys()]


SCATTER_PICK_FUNCS = {
    "Alternate": pick_alternate,
    "Random": pick_random,
    "Shuffle Cycle": pick_shuffle_cycle,
}

# -----------------------------
# Property Names (for preset save/load)
# -----------------------------

SCATTER_PROPERTY_NAMES = [
    "points_distance",
    "random_points_distance",
    "random_points_distance_range",
    "points_distance_percent",
    "position_offset",
    "random_scale",
    "random_scale_min",
    "random_scale_max",
    "random_scale_uniform",
    "random_scale_uniform_range",
    "random_rot",
    "random_rot_range",
    "random_flip",
    "align_to_tangent",
    "align_to_normal",
    "forward_axis",
    "up_axis",
    "normal_angle",
    "normal_offset",
    "normal_offset_percent",
    "normal_offset_random",
    "binormal_offset",
    "binormal_offset_percent",
    "binormal_offset_random",
    "use_collections",
    "is_multi_obj_scatter",
    "copy_data",
    "select_result_clones",
    "straighten",
    "pick_func",
]


def get_property_values(obj):
    """Extract all scatter property values from an object"""
    return {
        key: list(getattr(obj, key))
        if hasattr(getattr(obj, key), "__iter__")
        and not isinstance(getattr(obj, key), str)
        else getattr(obj, key)
        for key in SCATTER_PROPERTY_NAMES
    }


def set_property_values(obj, values):
    """Set all scatter property values on an object"""
    for key, value in values.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


# -----------------------------
# Preset Operators
# -----------------------------


class OBJECT_OT_scatter_save_preset(Operator):
    """Save current settings as a preset"""

    bl_idname = "object.scatter_save_preset"
    bl_label = "Save Preset"

    preset_name: StringProperty(name="Preset Name", default="New Preset")

    def execute(self, context):
        if not self.preset_name.strip():
            self.report({"ERROR"}, "Preset name cannot be empty")
            return {"CANCELLED"}

        # Get the active operator (the scatter operator in redo panel)
        active_op = context.active_operator
        if active_op and active_op.bl_idname == "object.scatter_on_curve":
            preset_data = get_property_values(active_op)
            save_preset(self.preset_name, preset_data)
            self.report({"INFO"}, f"Preset '{self.preset_name}' saved")
        else:
            self.report({"ERROR"}, "No active scatter operator found")
            return {"CANCELLED"}

        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)


class OBJECT_OT_scatter_load_preset(Operator):
    """Load a preset"""

    bl_idname = "object.scatter_load_preset"
    bl_label = "Load Preset"

    preset_name: StringProperty()

    def execute(self, context):
        preset_data = load_preset(self.preset_name)

        if preset_data is None:
            self.report({"ERROR"}, f"Preset '{self.preset_name}' not found")
            return {"CANCELLED"}

        # Apply to active operator
        active_op = context.active_operator
        if active_op and active_op.bl_idname == "object.scatter_on_curve":
            set_property_values(active_op, preset_data)
            self.report({"INFO"}, f"Preset '{self.preset_name}' loaded")

        return {"FINISHED"}


class OBJECT_OT_scatter_delete_preset(Operator):
    """Delete a preset"""

    bl_idname = "object.scatter_delete_preset"
    bl_label = "Delete Preset"

    preset_name: StringProperty()

    def execute(self, context):
        delete_preset(self.preset_name)
        self.report({"INFO"}, f"Preset '{self.preset_name}' deleted")
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)


# -----------------------------
# Main Operator
# -----------------------------


class OBJECT_OT_scatter_on_curve(Operator):
    bl_idname = "object.scatter_on_curve"
    bl_label = "Scatter Objects on Curve"
    bl_options = {"REGISTER", "UNDO"}

    # Distance
    points_distance: FloatProperty(name="Points Distance", default=1.0, min=0.001)
    random_points_distance: BoolProperty(name="Randomize Distance", default=False)
    random_points_distance_range: FloatVectorProperty(
        name="Random Dist Range", default=(1.0, 5.0), size=2
    )
    points_distance_percent: FloatProperty(
        name="Distance Percent", default=0.0, min=0.0
    )
    position_offset: FloatVectorProperty(
        name="Position Offset", default=(0.0, 0.0, 0.0), size=3, subtype="XYZ"
    )

    # Scale
    random_scale: BoolProperty(name="Random Scale", default=False)
    random_scale_min: FloatVectorProperty(
        name="Random Scale Min", default=(0.2, 0.2, 0.2), size=3, subtype="XYZ"
    )
    random_scale_max: FloatVectorProperty(
        name="Random Scale Max", default=(1.2, 1.2, 1.2), size=3, subtype="XYZ"
    )
    random_scale_uniform: BoolProperty(name="Uniform Scale", default=True)
    random_scale_uniform_range: FloatVectorProperty(
        name="Uniform Scale Range", default=(0.2, 1.2), size=2
    )

    # Rotation
    random_rot: BoolProperty(name="Random Rotation", default=False)
    random_rot_range: FloatVectorProperty(
        name="Random Rot Range", default=(1.0, 1.0, 1.0), size=3, subtype="XYZ"
    )
    random_flip: FloatVectorProperty(
        name="Random Rot Flip", default=(0, 0, 0), size=3, subtype="XYZ"
    )

    align_to_tangent: FloatProperty(
        name="Align to Tangent", default=0.0, min=-1.0, max=1.0
    )
    align_to_normal: FloatProperty(
        name="Align to Normal", default=0.0, min=-1.0, max=1.0
    )
    forward_axis: EnumProperty(
        name="Forward Axis",
        items=[
            ("X", "X", "Positive X"),
            ("Y", "Y", "Positive Y"),
            ("Z", "Z", "Positive Z"),
            ("-X", "-X", "Negative X"),
            ("-Y", "-Y", "Negative Y"),
            ("-Z", "-Z", "Negative Z"),
        ],
        default="-Y",
    )
    up_axis: EnumProperty(
        name="Up Axis",
        items=[
            ("X", "X", "Positive X"),
            ("Y", "Y", "Positive Y"),
            ("Z", "Z", "Positive Z"),
            ("-X", "-X", "Negative X"),
            ("-Y", "-Y", "Negative Y"),
            ("-Z", "-Z", "Negative Z"),
        ],
        default="Z",
    )
    normal_angle: FloatProperty(
        name="Normal Angle", default=math.radians(90), subtype="ANGLE"
    )
    normal_offset: FloatProperty(name="Normal Offset", default=0.0)
    normal_offset_percent: FloatProperty(name="Normal Offset %", default=0.0)
    normal_offset_random: FloatProperty(name="Normal Offset Random", default=0.0)
    binormal_offset: FloatProperty(name="Binormal Offset", default=0.0)
    binormal_offset_percent: FloatProperty(name="Binormal Offset %", default=0.0)
    binormal_offset_random: FloatProperty(name="Binormal Offset Random", default=0.0)

    # Misc
    use_collections: BoolProperty(name="Use Collections", default=False)
    is_multi_obj_scatter: BoolProperty(name="Multi Object Scatter", default=True)
    copy_data: BoolProperty(name="Copy Data", default=False)
    select_result_clones: BoolProperty(name="Select Results", default=True)
    straighten: BoolProperty(name="Straighten", default=False)

    scatter_pick_method: EnumProperty(
        name="Pick",
        description="Choose a method to pick objects to be scattered",
        items=get_scatter_pick_items,
    )

    def draw(self, context):
        layout = self.layout

        # Preset section at the top
        box = layout.box()
        box.label(text="Presets", icon="PRESET")

        presets = get_all_preset_names()
        if presets:
            col = box.column(align=True)
            for preset_name in presets:
                row = col.row(align=True)
                op = row.operator(
                    "object.scatter_load_preset", text=preset_name, icon="IMPORT"
                )
                op.preset_name = preset_name
                op = row.operator("object.scatter_delete_preset", text="", icon="X")
                op.preset_name = preset_name

        box.operator("object.scatter_save_preset", icon="ADD")

        # Regular settings below
        layout.separator()
        layout.label(text="Distance", icon="DRIVER_DISTANCE")
        layout.prop(self, "points_distance")
        layout.prop(self, "random_points_distance")
        if self.random_points_distance:
            layout.prop(self, "random_points_distance_range")
        layout.prop(self, "points_distance_percent")
        layout.prop(self, "position_offset")

        layout.separator()
        layout.label(text="Scale", icon="FULLSCREEN_EXIT")
        layout.prop(self, "random_scale")
        if self.random_scale:
            layout.prop(self, "random_scale_uniform")
            if self.random_scale_uniform:
                layout.prop(self, "random_scale_uniform_range")
            else:
                layout.prop(self, "random_scale_min")
                layout.prop(self, "random_scale_max")

        layout.separator()
        layout.label(text="Rotation", icon="ORIENTATION_GIMBAL")
        layout.prop(self, "random_rot")
        if self.random_rot:
            layout.prop(self, "random_rot_range")
            layout.prop(self, "random_flip")
        layout.prop(self, "align_to_tangent")
        layout.prop(self, "align_to_normal")
        layout.prop(self, "forward_axis")
        layout.prop(self, "up_axis")
        layout.prop(self, "normal_angle")

        layout.separator()
        layout.label(text="Offsets", icon="EMPTY_ARROWS")
        layout.prop(self, "normal_offset")
        layout.prop(self, "normal_offset_percent")
        layout.prop(self, "normal_offset_random")
        layout.prop(self, "binormal_offset")
        layout.prop(self, "binormal_offset_percent")
        layout.prop(self, "binormal_offset_random")

        layout.separator()
        layout.label(text="Misc", icon="PREFERENCES")
        layout.prop(self, "use_collections")
        layout.prop(self, "is_multi_obj_scatter")
        layout.prop(self, "copy_data")
        layout.prop(self, "select_result_clones")
        layout.prop(self, "straighten")
        layout.prop(self, "scatter_pick_method")

    def execute(self, context):
        scatter_objects_on_curve(
            points_distance=self.points_distance,
            random_points_distance=self.random_points_distance,
            random_points_distance_range=Vector(self.random_points_distance_range),
            points_distance_percent=self.points_distance_percent,
            position_offset=Vector(self.position_offset),
            random_scale=self.random_scale,
            random_scale_min=Vector(self.random_scale_min),
            random_scale_max=Vector(self.random_scale_max),
            random_scale_uniform=self.random_scale_uniform,
            random_scale_uniform_range=Vector(self.random_scale_uniform_range),
            random_rot=self.random_rot,
            random_rot_range=Vector(self.random_rot_range),
            random_flip=Vector(self.random_flip),
            align_to_tangent=self.align_to_tangent,
            align_to_normal=self.align_to_normal,
            forward_axis=self.forward_axis,
            up_axis=self.up_axis,
            normal_angle=self.normal_angle,
            normal_offset=self.normal_offset,
            normal_offset_percent=self.normal_offset_percent,
            normal_offset_random=self.normal_offset_random,
            binormal_offset=self.binormal_offset,
            binormal_offset_percent=self.binormal_offset_percent,
            binormal_offset_random=self.binormal_offset_random,
            use_collections=self.use_collections,
            is_multi_obj_scatter=self.is_multi_obj_scatter,
            copy_data=self.copy_data,
            select_result_clones=self.select_result_clones,
            straighten=self.straighten,
            pick_func=SCATTER_PICK_FUNCS.get(self.scatter_pick_method),
        )

        self.report({"INFO"}, "Scatter executed")
        return {"FINISHED"}

class ScatterObjectsPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        layout = self.layout
        
        # Main Header Box
        box = layout.box()
        box.label(text="How to Use This Addon:", icon='QUESTION')
        
        # Location Instructions
        col = box.column(align=True)
        col.label(text="📍 Location: Search Menu (F3) or Object Menu", icon='MARKER')
        col.separator()
        
        # Step-by-step instructions
        col.label(text="1. Select one or more mesh objects you want to scatter.")
        col.label(text="2. Hold Shift and select your target Curve object LAST (making it the active object).")
        col.label(text="3. Press F3 (or Spacebar) and search for 'Scatter Objects on Curve'.")
        col.label(text="4. Adjust options like distance, rotation, and scale in the Adjust Last Operation popup (bottom-left).")
        
        # Warning/Note box
        box_note = box.box()
        box_note.label(text="⚠️ Note: Make sure to Apply Scale (Ctrl + A) to both the curve and mesh objects before scattering!", icon='ERROR')

# -----------------------------
# Registration
# -----------------------------


def menu_func(self, context):
    self.layout.operator(OBJECT_OT_scatter_on_curve.bl_idname)


classes = (
    OBJECT_OT_scatter_on_curve,
    OBJECT_OT_scatter_save_preset,
    OBJECT_OT_scatter_load_preset,
    OBJECT_OT_scatter_delete_preset,
    ScatterObjectsPreferences
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_MT_object.append(menu_func)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    bpy.types.VIEW3D_MT_object.remove(menu_func)


if __name__ == "__main__":
    register()
