# Frame Expansion Compensation
#
# Copyright (C) 2021  Robert Pazdzior <robertp@norbital.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Compensates for thermal expansion induced toolhead Z movement in real-time
# using a frame-coupled temperature probe.

KELVIN_TO_CELSIUS = -273.15
STD_TEMP = 25. # reference temperature for frame measurement

class FrameExpansionCompensator:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')

        # Get config parameters, convert to SI units where necessary
        self.coeff = config.getfloat('coeff', minval=0., maxval=100.,)/1E6
        self.frame_z = config.getfloat('frame_z_length', minval=0.,)/1E3
        self.gantry_factor = config.getfloat('gantry_factor', default=1.0)
        self.min_temp = config.getfloat('min_temp', minval=KELVIN_TO_CELSIUS)
        self.max_temp = config.getfloat('max_temp', above=self.min_temp)
        self.z_stepper_name = config.get('z_stepper')
        self.max_comp_z = config.getfloat('max_comp_z', 0.)
        self.max_offset = config.getfloat('max_z_offset', 99999999.)

        # Register printer events
        self.printer.register_event_handler("klippy:ready",
                                            self.handle_ready)
        self.printer.register_event_handler("homing:homing_move_end",
                                            self.handle_homing_move_end)

        # Setup temperature sensor
        self.last_temp = 0.
        self.measured_min = 99999999.
        self.measured_max = 0.
        self.last_home_temp = 0.
        pheaters = self.printer.load_object(config, 'heaters')
        self.sensor = pheaters.setup_sensor(config)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        pheaters.register_sensor(config, self)

        # Z offset transformation
        self.z_drift_offset = 0.
        self.comp_enable = True
        self.last_position = [0., 0., 0., 0.]
        self.next_transform = None
        self.comp_state = 'not homed'

        # Register gcode commands
        self.gcode.register_command('SET_FRAME_COMP',
                                    self.cmd_SET_FRAME_COMP,
                                    desc=self.cmd_SET_FRAME_COMP_help)
        self.gcode.register_command('QUERY_FRAME_COMP',
                                    self.cmd_QUERY_FRAME_COMP,
                                    desc=self.cmd_QUERY_FRAME_COMP_help)

    def handle_ready(self):
        'Called when Klipper successfully connected, ready.'
        self.toolhead = self.printer.lookup_object('toolhead')
        gcode_move = self.printer.lookup_object('gcode_move')

        # Z stepper config check
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        steppers = [s.get_name() for s in kin.get_steppers()]
        if not self.z_stepper_name in steppers:
            raise self.printer.config_error(
                "'%s' is not a valid stepper" % self.z_stepper_name)

        # Transformation
        self.next_transform = gcode_move.set_move_transform(self, force=True)

    def get_temp(self, eventtime):
        'Used by M105, for example, to query probe temperature.'
        return self.last_temp, 0

    def stats(self, eventtime):
        return False, '%s: temp=%.1f' % ('Frame', self.last_temp)

    def get_status(self, eventtime):
        return {
            'temperature': self.last_temp,
            'measured_min_temp': self.measured_min,
            'measured_max_temp': self.measured_max,
            'current_z_comp': self.z_drift_offset,
            'frame_ref_temp': self.last_home_temp,
            'state': self.comp_state
        }

    def check_eligible(self, endstops):
        'Check if homed axis is `stepper_z`.'
        steppers_being_homed = [s.get_name()
                                for es in endstops
                                for s in es.get_steppers()]
        return self.z_stepper_name in steppers_being_homed

    def handle_homing_move_end(self, hmove):
        'Triggered when Z axis is homed.'
        if self.check_eligible(hmove.get_mcu_endstops()):
            self.last_home_temp = self.last_temp
            self.z_drift_offset = 0.

    def calc_offset(self, pos):
        'Calculate total linear thermal expansion relative to last homing.'
        if not self.max_comp_z or pos[2] < self.max_comp_z:
            # Calculate the extrusion length at homing temperature
            length_home = (self.frame_z + self.frame_z * ( self.coeff *
                            (self.last_home_temp - STD_TEMP) ))
            delta_t = self.last_temp - self.last_home_temp

            # Calculate Z offset, compensates for thermal expansion since homing
            offset = -1 * (length_home *
                (self.coeff * delta_t) * self.gantry_factor) * 1E3

            # compute sign (+1 or -1) for maximum offset setting
            sign = 1 - (offset <= 0)*2

            self.z_drift_offset = min([self.max_offset*sign, offset], key=abs)

        self.update_state()

        # Apply offset
        new_z = pos[2] + self.z_drift_offset
        return [pos[0], pos[1], new_z, pos[3]]

    def calc_unoffset(self, pos):
        'Remove Z-drift offset'
        unoffset_z = pos[2] - self.z_drift_offset
        return [pos[0], pos[1], unoffset_z, pos[3]]

    def get_position(self):
        return self.calc_unoffset(self.next_transform.get_position())

    def move(self, newpos, speed):
        # don't apply to extrude only moves or when disabled
        if (newpos[0:2] == self.last_position[0:2]) or not self.comp_enable:
            self.next_transform.move(newpos, speed)
        else:
            corrected_pos = self.calc_offset(newpos)
            self.next_transform.move(corrected_pos, speed)
            self.last_position[:] = newpos
        self.last_position[:] = newpos

    def temperature_callback(self, read_time, temp):
        'Called everytime the thermistor is read'
        self.last_temp = temp
        if temp:
            self.measured_min = min(self.measured_min, temp)
            self.measured_max = max(self.measured_max, temp)

    def update_state(self):
        'String output for g-code and get_status state tracking.'
        if self.last_home_temp:
            state = 'Enabled' if self.comp_enable else "Disabled"
        else:
            state = 'Not homed'
            self.z_drift_offset = 0

        if self.max_comp_z and self.last_position[2] > self.max_comp_z:
            state = 'Disabled above Z=%.3f' % self.max_comp_z

        if self.z_drift_offset > self.max_offset:
            state += ', offset limit (%.5fmm) reached' % self.max_offset

        self.comp_state = state

    def cmd_SET_FRAME_COMP(self, gcmd):
        new_state = gcmd.get_int('ENABLE', 1, minval=0, maxval=1)
        if new_state:
            gcmd.respond_info('Z drift compensation ENABLED')
        else:
            gcmd.respond_info('Z drift compensation DISABLED')
        if new_state != self.comp_enable:
            gcode_move = self.printer.lookup_object('gcode_move')
            gcode_move.reset_last_position()
            self.comp_enable = new_state

    def cmd_QUERY_FRAME_COMP(self, gcmd):
        self.update_state()
        msg = ("Z Drift / Thermal Expansion Compensation:\n"
               "STATE: %s\n"
               "FRAME TEMPERATURE: ref=%.2f; now=%.2f\n"
               "TOTAL Z COMP: z=%.4f mm" % (self.comp_state,
                                            self.last_home_temp,
                                            self.last_temp,
                                            self.z_drift_offset)
        )
        gcmd.respond_info(msg)
    cmd_SET_FRAME_COMP_help = 'Enable/disable Z drift compensation.'
    cmd_QUERY_FRAME_COMP_help = 'Report current compensation parameters.'

def load_config(config):
    return FrameExpansionCompensator(config)
