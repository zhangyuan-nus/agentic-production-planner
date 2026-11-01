"""A stand-in for the team's four-factory SimPy testbed, for trying the planner end to end.

It speaks the same MQTT contract (``Factory/<n>/Information`` out, ``optimizer/instructions``
in) and models what the planner reasons about: machine states and rates, the 10 s start-up,
and combiners that consume their inputs. It leaves out the full testbed's internal conveyors,
lorry travel and anomaly scenarios.
"""
