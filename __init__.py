import logging
import RHUtils
import json
import requests
from eventmanager import Evt
import Config
from RHUI import UIField, UIFieldType, UIFieldSelectOption
import struct
from time import monotonic
from Database import ProgramMethod
import gevent.monkey
gevent.monkey.patch_all()

logger = logging.getLogger(__name__)

#UI and db inputs
PANEL_NAME = "FlowStatePanel"
SERVER_TICK_RATE_INPUT = "FSServerTickRate"
AUTO_RUN_INPUT = "FSAutoRun"
CLIENT_TICK_RATE_INPUT = "FSClientTickRate"
CLIENT_JITTER_COMP_INPUT = "FSClientJitterComp"
TRACK_INPUT = "FSTrack"
LAP_DELAY_TIME_INPUT = "FSLapDelayTime"
RACE_COOLDOWN_TIME_INPUT = "FSRaceCooldown"
APPLY_INPUT = "FSApply"
HEAT_LOCK_INPUT = "FSHeatLockInput"


STEAM_ID = "SteamID"
UPDATE_TIMEOUT = 5
MAX_PLAYERS = 8
MAX_SPECTATORS = 8

#default values
DEFAULT_AUTO_RUN = "0"
DEFAULT_SERVER_TICK_RATE = 10
DEFAULT_CLIENT_TICK_RATE = 10
DEFAULT_CLIENT_JITTER_COMP = 50
DEFAULT_TRACK = "The Shrine"
DEFAULT_LAP_DELAY_TIME = 999
DEFAULT_RACE_COOLDOWN_TIME = 30
DEFAULT_HEAT_LOCK = "0"
DEFAULTS = {
    SERVER_TICK_RATE_INPUT: DEFAULT_SERVER_TICK_RATE,
    AUTO_RUN_INPUT: DEFAULT_AUTO_RUN,
    CLIENT_TICK_RATE_INPUT: DEFAULT_CLIENT_TICK_RATE,
    CLIENT_JITTER_COMP_INPUT: DEFAULT_CLIENT_JITTER_COMP,
    TRACK_INPUT: DEFAULT_TRACK,
    LAP_DELAY_TIME_INPUT: DEFAULT_LAP_DELAY_TIME,
    RACE_COOLDOWN_TIME_INPUT: DEFAULT_RACE_COOLDOWN_TIME,
    HEAT_LOCK_INPUT: DEFAULT_HEAT_LOCK
}

def initialize(rhapi):
    RH = FSManager(rhapi)

    logging.info("--------------INITIALIZE FLOW STATE--------------")
    
    rhapi.ui.register_panel(PANEL_NAME, 'FlowState', 'run', order=0)

    clientTickRateField = UIField(name = CLIENT_TICK_RATE_INPUT, label = 'Client Tick Rate', field_type = UIFieldType.BASIC_INT, value = DEFAULT_CLIENT_TICK_RATE)
    rhapi.fields.register_option(clientTickRateField, PANEL_NAME)

    jitterCompField = UIField(name = CLIENT_JITTER_COMP_INPUT, label = 'Client Smoothing (0-100)', field_type = UIFieldType.BASIC_INT, value = DEFAULT_CLIENT_JITTER_COMP)
    rhapi.fields.register_option(jitterCompField, PANEL_NAME)

    trackField = UIField(name = TRACK_INPUT, label = 'Track', field_type = UIFieldType.TEXT, value = DEFAULT_TRACK)
    rhapi.fields.register_option(trackField, PANEL_NAME)

    lapDelay = UIField(name = LAP_DELAY_TIME_INPUT, label = 'Lap Delay Ms (if a player\'s ping is higher than this value, laps may be read late)', field_type = UIFieldType.BASIC_INT, value = DEFAULT_LAP_DELAY_TIME)
    rhapi.fields.register_option(lapDelay, PANEL_NAME)
    
    raceCooldown = UIField(name = RACE_COOLDOWN_TIME_INPUT, label = 'Race Cooldown Time', field_type = UIFieldType.BASIC_INT, value = DEFAULT_RACE_COOLDOWN_TIME)
    rhapi.fields.register_option(raceCooldown, PANEL_NAME)

    lockHeat = UIField(name = HEAT_LOCK_INPUT, label = 'Lock Heat (prevent player from joining/leaving heats)', field_type = UIFieldType.CHECKBOX, value = DEFAULT_HEAT_LOCK)
    rhapi.fields.register_option(lockHeat, PANEL_NAME)

    autoRun = UIField(name = AUTO_RUN_INPUT, label = 'Auto Run Next Heat', field_type = UIFieldType.CHECKBOX, value = DEFAULT_AUTO_RUN)
    rhapi.fields.register_option(autoRun, PANEL_NAME)
    
    rhapi.ui.register_quickbutton(PANEL_NAME, APPLY_INPUT, 'Apply', RH.apply)

    #data attributes
    pilotSteamID = UIField(name = STEAM_ID, label = "Steam ID", field_type = UIFieldType.TEXT)
    rhapi.fields.register_pilot_attribute(pilotSteamID)

    RH.loadDefaults()
    
    logging.info("--------------FLOW STATE INITIALIZED--------------")

class FSManager():
    def __init__(self, rhapi):
        self.rhapi = rhapi
        self.maxPlayerCount = MAX_PLAYERS
        self.maxSpectatorCount = MAX_SPECTATORS
        
        #websocket listeners
        self.rhapi.ui.socket_listen("fs_set_state", self.setPlayerState)
        self.rhapi.ui.socket_listen("fs_get_settings", self.setClientSettings)
        self.rhapi.ui.socket_listen("fs_player_join", self.handlePlayerJoin)
        self.rhapi.ui.socket_listen("fs_request_seat", self.handleSeatRequest)
        self.rhapi.ui.socket_listen("fs_request_spectate", self.handleSpectateRequest)
        self.rhapi.ui.socket_listen("fs_spectate", self.handleSpectate)
        self.rhapi.ui.socket_listen("fs_add_lap", self.handleNewLap)

        #main game state that will be distributed to all players as well as updated by them
        self.flowState = {"time":0.0,"states":[]}
        self.flowStateMeta = []
        self.spectatorMeta = []
        self.cachedLaps = []


        for i in range(0, self.maxPlayerCount):
            blankState = {"seat": -1, "position":[0,-100,0], "orientation":[0,0,0], "rssi":0}
            self.flowState["states"].append(blankState)
            blankMeta = {"lastUpdateTime":0.0, "steamId": ""}
            self.flowStateMeta.append(blankMeta)
            self.cachedLaps.append([])

        for i in range(0, self.maxSpectatorCount):
            blankMeta = {"lastUpdateTime":0.0, "steamId": ""}
            self.spectatorMeta.append(blankMeta)

        self.lastTick = monotonic()
    def loadDefaults(self):
        #load default values
        #if(self.getOption(LAP_DELAY_TIME_INPUT)==None):
        #    self.setOption(LAP_DELAY_TIME_INPUT, DEFAULT_LAP_DELAY_TIME)

        #if(self.getOption(SERVER_TICK_RATE_INPUT)==None):
        #    self.setOption(SERVER_TICK_RATE_INPUT, DEFAULT_SERVER_TICK_RATE)

        #if(self.getOption(CLIENT_TICK_RATE_INPUT)==None):
        #    self.setOption(CLIENT_TICK_RATE_INPUT, DEFAULT_SERVER_TICK_RATE)

        #if(self.getOption(CLIENT_JITTER_COMP_INPUT)==None):
        #    self.setOption(CLIENT_JITTER_COMP_INPUT, DEFAULT_CLIENT_JITTER_COMP)
        
        #if(self.getOption(TRACK_INPUT)==None):
        #    self.setOption(TRACK_INPUT, DEFAULT_TRACK)

        #if(self.getOption(RACE_COOLDOWN_TIME_INPUT)==None):
        #    self.setOption(RACE_COOLDOWN_TIME_INPUT, DEFAULT_RACE_COOLDOWN_TIME)
        pass
    
    def getOption(self, option):
        if(self.rhapi.db.option(option)==None):
            default = DEFAULTS[option]
            self.setOption(option, default)
        return self.rhapi.db.option(option)

    def setOption(self, option, value):
        self.rhapi.db.option_set(option, value)

    def handleAutoRun(self):
        if(self.rhapi.db.option(AUTO_RUN_INPUT)=="1"):
            #if the race is in the stopped state
            if(self.rhapi.race.status==2):
                #if a heat hasn't been scheduled yet
                if(self.rhapi.race.scheduled==None):
                    #get the current heat
                    currentHeat = self.rhapi.db.heat_by_id(self.rhapi.race.heat)

                    #save the race that was just completed
                    self.rhapi.race.save()

                    #create a new heat
                    newHeat = self.rhapi.db.heat_add(name=None, raceclass=currentHeat.class_id, auto_frequency=False)

                    #set the current heat to the new heat
                    self.rhapi.race.heat = newHeat.id

                    #update the user interface
                    self.rhapi.ui.broadcast_raceclasses()
                    self.rhapi.ui.broadcast_current_heat()
      
                    #iterate over our connected pilots
                    logging.info(str(self.flowStateMeta))
                    for seat in range(0,len(self.flowStateMeta)):
                        
                        #find pilot id via steam id
                        meta = self.flowStateMeta[seat]
                        steamID = meta["steamId"]
                        if(steamID!=""):
                            pilotsWithSteamID = self.rhapi.db.pilot_ids_by_attribute(STEAM_ID,steamID)

                            #found a pilot with a steam ID
                            if(len(pilotsWithSteamID)>0):
                                pilotID = pilotsWithSteamID[0]
                                self.addPilotToCurrentHeat(pilotID)

                    #schedule the next heat
                    self.rhapi.race.schedule(self.getOption(RACE_COOLDOWN_TIME_INPUT))

            self.handleEarlyFinish()

    def handleNewLap(self,data):
        seat = data["seat"]
        time = data["time"]
        lapDelay = self.getOption(LAP_DELAY_TIME_INPUT)
        logging.info("lap delay: "+str(lapDelay))
        gevent.spawn(self.addLapInFuture, seat, time+(int(lapDelay)/1000))

    def addLapInFuture(self, node, time):
        if(monotonic()>time):
            message = "WARNING! Player on node "+str(node+1)+" logged a lap that arrived "+str(monotonic()-time)+"s late"
            logging.info(message)

            self.rhapi.ui.message_speak("Warning! Lag detected when counting lap for node "+str(node+1)+". Please increase lap delay, or check if the server needs more resources.")
        else:
            logging.info("Logging a lap time for node "+str(node+1)+" in "+str(time-+monotonic())+"s")
        while True:
            if(monotonic()>=time):
                self.addLap(node, time)
                logging.info("time: "+str(time))
                break
                
            gevent.sleep()

    def addLap(self, node, time):
        addTime = monotonic()
        self.rhapi.interface.simulate_lap({"node":node})
        logging.info("Lap was added "+str((addTime-time)*1000)+"ms late")

    def handleSeatRequest(self, data):
        logging.info("pilot "+str(data['pilotId'])+" requested to be join the current heat")
        self.addPilotToCurrentHeat(data['pilotId'])

    def handleSpectateRequest(self, data):
        logging.info(data)
        logging.info("pilot "+str(data['pilotId'])+" requested to be removed from the current heat")
        self.removePilotFromCurrentHeat(data['pilotId'])

    def getConnectedSeats(self):
        connectedSeats = []
        for i in range(0,len(self.flowStateMeta)):
            sto = self.flowStateMeta[i]["lastUpdateTime"]
            
            if(monotonic()-sto<UPDATE_TIMEOUT):
                connectedSeats.append(True)
            else:
                connectedSeats.append(False)
                self.flowState['states'][i] = {"seat": -1, "position":[0,-100,0], "orientation":[0,0,0], "rssi":0}
        return connectedSeats

    def handleEarlyFinish(self):
        seatsConnected = self.getConnectedSeats()
        #if the race is currently running
        if(self.rhapi.race.status==1):
            seatsFinished = self.rhapi.race.seats_finished

            #check if all the connected pilots are done
            readyToRestart = True
            for i in range(0,len(seatsConnected)):
                connected = seatsConnected[i]
                if(i in seatsFinished):
                    finished = seatsFinished[i]
                    if((connected) and (not finished)):
                        readyToRestart = False
                        break
            #if so...
            if(readyToRestart):
                #stop the race early
                self.rhapi.race.stop()

    def findOpenSeat(self):
        logging.info("findOpenSeat")

        currentHeatID = self.rhapi.race.heat
        logging.info("- "+str(currentHeatID))
        slots = self.rhapi.db.slots_by_heat(currentHeatID)
        logging.info("slots: "+str(slots))
        openSeat = -1
        for slot in slots:
            logging.info(slot.pilot_id)
            if(slot.pilot_id==0):
                logging.info("found open slot! "+str(slot.node_index))
                openSeat = slot.node_index
                break
        
        return openSeat

    def handlePlayerJoin(self, data):
        logging.info("handlePlayerJoin")
        logging.info("seats already connected...")
        logging.info(str(self.getConnectedSeats()))
        logging.info("searching for pilot...")
        foundPilotsWithSteamID = self.rhapi.db.pilot_ids_by_attribute(STEAM_ID,data["steamId"])
        foundPilot = None
        if(len(foundPilotsWithSteamID)>0):
            logging.info("found matching pilot! "+str(foundPilotsWithSteamID))
            for pilot in foundPilotsWithSteamID:
                if(pilot!=None):
                    foundPilot = self.rhapi.db.pilot_by_id(pilot)
                    if(foundPilot!=None):
                        foundPilot.callsign = data["steamName"]
                        break

        #this pilot doesn't exist in the system yet. Let's add them
        if(foundPilot==None):
            foundPilot = self.rhapi.db.pilot_add(name=data["steamName"], callsign=data["steamName"], phonetic=None, team=None, color=None)
            self.rhapi.db.pilot_alter(pilot_id=foundPilot.id, attributes={STEAM_ID:data["steamId"]})

            #update the user interface
            self.rhapi.ui.broadcast_pilots()

        #add the pilot to the current heat
        seat = self.addPilotToCurrentHeat(foundPilot.id)

        #add the player to the spectator or the seated list depending on if there was a seat available
        if(seat==-1):
            self.spectatorMeta[seat]["steamId"] = data["steamId"]
        else:
            self.flowStateMeta[seat]["steamId"] = data["steamId"]
        logging.info("pilot joined: "+str(foundPilot.callsign)+", "+str(foundPilot.id))
        self.rhapi.ui.socket_send("fs_join_success", {"pilotId":foundPilot.id, "seat":seat})

    def addPilotToCurrentHeat(self, pilotID):
        #set the player in their slot if they are already in the heat
        currentHeatID = self.rhapi.race.heat
        slots = self.rhapi.db.slots_by_heat(currentHeatID)
        foundSeat = -1
        logging.info("checking if race is stopped")
        #if we aren't racing
        if(self.rhapi.race.status!=1):
            logging.info("Looking for pilot "+str(pilotID)+" in current heat "+str(currentHeatID))
            for slot in slots:
                p = self.rhapi.db.pilot_by_id(slot.pilot_id)
                if(p!=None):
                    p = p.callsign
                logging.info("pilot "+str(p)+" is on slot "+str(slot.node_index))
                #if the new pilot is already set to a slot in the current heat
                if(slot.pilot_id==pilotID):
                    #player is in the heat twice!
                    if(foundSeat!=-1):
                        #remove the pilot from the slot
                        logging.info("removing pilot "+str(pilotID)+ " duplicate in heat "+str(currentHeatID)+", seat "+str(slot.node_index))
                        self.rhapi.db.slot_alter(slot.id, method=ProgramMethod.NONE, pilot=0, seed_heat_id=None, seed_raceclass_id=None, seed_rank=None)
                    else:
                        #mark this as the slot we will use
                        foundSeat = slot.node_index
                        logging.info("Pilot with ID "+str(pilotID)+ " is already in heat "+str(currentHeatID))

            #the player isn't in any of the heat's slots. Add them to an open slot if available
            if(foundSeat==-1):
                logging.info("pilot was not found in current heat")
                #find an open seat if available
                foundSeat = self.findOpenSeat()
                logging.info("found open slot "+str(foundSeat))

            #if we found a seat for the pilot, add him to it
            if(foundSeat!=-1):
                logging.info("adding pilot to heat "+str(currentHeatID)+" on seat "+str(foundSeat+1))
                #add the pilot to the current heat
                slot = slots[foundSeat]
                self.rhapi.db.slot_alter(slot.id, method=ProgramMethod.ASSIGN, pilot=pilotID, seed_heat_id=None, seed_raceclass_id=None, seed_rank=None)

            #update user interface
            self.rhapi.ui.broadcast_race_status()
            self.rhapi.ui.broadcast_current_heat()
            self.rhapi.ui.broadcast_heats()
            self.rhapi.ui.broadcast_raceclasses()
        else:
            logging.info("pilot "+str(pilotID)+" could not be added to the heat because a race is occuring")

        return foundSeat
    
    def removePilotFromCurrentHeat(self, pilotID):
        #set the player in their slot if they are already in the heat
        currentHeatID = self.rhapi.race.heat
        slots = self.rhapi.db.slots_by_heat(currentHeatID)
        foundSeat = -1
        logging.info("checking if race is stopped")
        #if we aren't racing
        if(self.rhapi.race.status!=1):
            logging.info("Looking for pilot "+str(pilotID)+" in current heat "+str(currentHeatID))
            for slot in slots:
                #if the new pilot is set to a slot in the current heat
                if(slot.pilot_id==pilotID):
                    #remove the pilot from the slot
                    logging.info("removing pilot "+str(pilotID)+ " duplicate in heat "+str(currentHeatID)+", seat "+str(slot.node_index))
                    self.rhapi.db.slot_alter(slot.id, method=ProgramMethod.NONE, pilot=0, seed_heat_id=None, seed_raceclass_id=None, seed_rank=None)

            #update user interface
            self.rhapi.ui.broadcast_race_status()
            self.rhapi.ui.broadcast_current_heat()
            self.rhapi.ui.broadcast_heats()
            self.rhapi.ui.broadcast_raceclasses()
        else:
            logging.info("pilot "+str(pilotID)+" could not be removed from the heat because a race is occuring")

    def handleSpectate(self):
        #echo the flow state
        self.flowState["time"] = monotonic()
        self.rhapi.ui.socket_send("fs", self.flowState)

        #WE GOTTA FIGURE OUT WHAT WE WANNA DO WITH SPECTATORS

        #let's keep track of when this player was last updated
        #self.spectatorMeta[seat]["lastUpdateTime"] = self.flowState["time"]

        self.handleAutoRun()

    def setPlayerState(self, data):
        stateArrivalTime = monotonic()
        
        #logging.info(str(data))
        seat = data["seat"]
        rssi = data["rssi"]

        #update state
        self.flowState["states"][seat] = data

        #echo the flow state
        self.flowState["time"] = monotonic()
        self.rhapi.ui.socket_send("fs", self.flowState)

        self.setRSSI(seat, rssi)

        #let's keep track of when this player was last updated
        self.flowStateMeta[seat]["lastUpdateTime"] = stateArrivalTime

        #handle tasks that need to run every time we get a client update
        self.handleAutoRun()
        #logging.info(self.flowState["time"])
        
        
    def setRSSI(self, seat, value):
        interface = self.rhapi.interface
        nodes = interface.seats
        nodes[seat].current_rssi = value
    
    def setClientSettings(self):
        logging.info("setClientSettings")
        #TO-DO get rid of async state
        serverSettings = {"track":self.getOption(TRACK_INPUT), "serverTickRate": self.getOption(SERVER_TICK_RATE_INPUT), "clientTickRate": self.getOption(CLIENT_TICK_RATE_INPUT), "jitterDampening": (100.0-float(self.getOption(CLIENT_JITTER_COMP_INPUT)))/100.0, "asyncState": True}
        self.rhapi.ui.socket_broadcast("fs_server_settings", serverSettings)

    def apply(self, args):
        logging.info("apply")
        self.setClientSettings()
