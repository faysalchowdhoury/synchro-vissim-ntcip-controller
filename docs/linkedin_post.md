The offsets with the widest green band made one direction of my arterial 60% worse.

I built a tool chain that takes a Synchro UTDF timing file, turns it into an emulated NEMA TS2 dual-ring controller (force-offs, gap-out, barrier hold, dual entry, early return), and then retimes a 4-signal corridor in closed loop. Each controller is also exposed over NTCIP 1202 (SNMP).

Comparing three offset plans (10 random seeds, 1 hour each):

🔹 Existing simultaneous offsets: 0 s green band, 25.8 s/veh
🔹 Max-bandwidth offsets: a 41 s eastbound green wave, but westbound delay +60%
🔹 Delay-optimised offsets: 24 s EB + 10 s WB band, all-movement delay -20% (20.6 s/veh, p < 0.001)

What I took away:

1️⃣ Bandwidth is a geometry objective, not a delay objective. With 420/400/530 m spacing and a 100 s cycle, the corridor can't carry a wide two-way band, so a MAXBAND-style optimiser spends it all on one direction. That's why Synchro optimises offsets on delay.

2️⃣ Validate the simulation against the deterministic method. On coordinated phases, HCM 7 delay (computed with simulated green and arrivals on green) matched the simulation within 2 s. Side streets differed by 8-15 s, and I can explain why: queue delay vs. control delay, plus random overflow.

3️⃣ Keep tuning and test seeds separate. The delay optimiser tuned on two seeds; results come from ten different ones. Same discipline as a train/test split.

On the systems side: the controller runs at 5.9 µs per 0.1 s tick, offset optimisation takes 1.6 s, and SNMP status polls return in 0.41 ms p50 (loopback). Python, NumPy, Pydantic, asyncio UDP with a hand-written BER codec, FastAPI, Docker, 23 pytest cases including a randomised barrier-safety property test.

Caveat: the corridor and UTDF file were built for this study. I had no Synchro or Vissim licence, so the Vissim bridge is untested and the numbers come from my own mesoscopic model.

Repo: [link]

Signal timing folks: when an arterial can't progress both ways, how do you choose which direction gets the band?

#TrafficEngineering #SignalTiming #Synchro #NTCIP #ITS #TransportationEngineering #Python #SystemDesign
