import logging
import RHUtils
import json
import requests
from eventmanager import Evt
import Config
from RHUI import UIField, UIFieldType, UIFieldSelectOption
import struct
from time import monotonic
import gevent.monkey
gevent.monkey.patch_all()

logger = logging.getLogger(__name__)

PANEL_NAME = "FlowStatePanel"
SERVER_TICK_RATE_INPUT = "ServerTickRateInput"
AUTO_RUN_INPUT = "AutoRun"
CLIENT_TICK_RATE_INPUT = "ClientTickRateInput"
CLIENT_JITTER_COMP_INPUT = "ClientJitterCompInput"
TRACK_INPUT = "TrackInput"
APPLY_INPUT = "ApplyInput"
STEAM_ID = "SteamID"
UPDATE_TIMEOUT = 5
MAX_PLAYERS = 8

def initialize(rhapi):
    logging.info("--------------INITIALIZE FLOW STATE--------------")
    RH = FSManager(rhapi)
    rhapi.ui.register_panel(PANEL_NAME, 'FlowState', 'run', order=0)

    clientTickRateField = UIField(name = CLIENT_TICK_RATE_INPUT, label = 'Client Tick Rate', field_type = UIFieldType.BASIC_INT, value = 20)
    rhapi.fields.register_option(clientTickRateField, PANEL_NAME)

    jitterCompField = UIField(name = CLIENT_JITTER_COMP_INPUT, label = 'Client Smoothing (0-100)', field_type = UIFieldType.BASIC_INT, value = 50)
    rhapi.fields.register_option(jitterCompField, PANEL_NAME)

    trackField = UIField(name = TRACK_INPUT, label = 'Track', field_type = UIFieldType.TEXT, value = "The Shrine")
    rhapi.fields.register_option(trackField, PANEL_NAME)

    autRun = UIField(name = AUTO_RUN_INPUT, label = 'Auto Run Next Heat', field_type = UIFieldType.CHECKBOX, value = "0")
    rhapi.fields.register_option(autRun, PANEL_NAME)

    rhapi.ui.register_quickbutton(PANEL_NAME, APPLY_INPUT, 'Apply', RH.apply)

    #data attributes
    pilotSteamID = UIField(name = STEAM_ID, label = "Steam ID", field_type = UIFieldType.TEXT)
    rhapi.fields.register_pilot_attribute(pilotSteamID)
    
    logging.info("--------------FLOW STATE INITIALIZED--------------")

class FSManager():
    def __init__(self, rhapi):
        self.rhapi = rhapi
        self.maxPlayerCount = MAX_PLAYERS
        
        #websocket listeners
        self.rhapi.ui.socket_listen("fs_set_state", self.setPlayerState)
        self.rhapi.ui.socket_listen("fs_get_settings", self.setClientSettings)
        self.rhapi.ui.socket_listen("fs_player_join", self.handlePlayerJoin)
        self.rhapi.ui.socket_listen("fs_spectate", self.handleSpectate)
        self.rhapi.ui.socket_listen("fs_add_lap", self.handleNewLap)

        #main game state that will be distributed to all players as well as updated by them
        self.blankState = {"seat": -1, "position":[0,-100,0], "orientation":[0,0,0], "rssi":0}
        self.flowState = {"time":0.0,"states":[self.blankState]*self.maxPlayerCount}
        blankMeta = {"lastUpdateTime":0.0}
        self.flowStateMeta = [blankMeta]*self.maxPlayerCount
        self.cachedLaps = [[[]]]*self.maxPlayerCount
        self.seatLastMessageTimes = [0,0,0,0,0,0,0,0]

        #load our server settings or fallback to default value
        logging.info("Flowstate: Loading server settings")
        serverTickRate = self.rhapi.db.option(SERVER_TICK_RATE_INPUT)
        clientTickRate = self.rhapi.db.option(CLIENT_TICK_RATE_INPUT)
        clientJitterCompensation = self.rhapi.db.option(CLIENT_JITTER_COMP_INPUT)
        track = self.rhapi.db.option(TRACK_INPUT)
        if(serverTickRate==None):
            logging.info("Flowstate: loading default server tick rate")
            serverTickRate = 50
        if(clientTickRate==None):
            logging.info("Flowstate: loading default server tick rate")
            clientTickRate = 50
        if(clientJitterCompensation==None):
            logging.info("Flowstate: loading default client smoothing")
            clientJitterCompensation = 75
        if(track==None):
            logging.info("Flowstate: loading default track state")
            track = "The Shrine"

        logging.info("Flowstate: fs object")
        self.serverTickRate = int(serverTickRate)
        self.clientTickRate = int(clientTickRate)
        self.clientJitterCompensation = int(clientJitterCompensation)
        self.track = track

        self.lastTick = monotonic()

    def handleAutoRun(self):
        if(self.rhapi.db.option(AUTO_RUN_INPUT)=="1"):
            #if the race is in the stopped state
            if(self.rhapi.race.status==2):
                #if a heat hasn't been scheduled yet
                if(self.rhapi.race.scheduled==None):
                    #save the laps and schedule the next heat
                    self.rhapi.race.save()
                    self.rhapi.race.schedule(11)

    def handleNewLap(self,data):
        seat = data["seat"]
        time = data["time"]
        #self.cachedLaps[seat].append(time)
        gevent.spawn(self.addLapInFuture, seat, time)
        #self.addLap(seat,time)

    def addLapInFuture(self, node, time):
        while True:
            if(monotonic()>=time):
                self.addLap(node, time)
                logging.info("time: "+str(time))
                break
                
            gevent.sleep()

    def addLap(self, node, time):
        addTime = monotonic()
        self.rhapi.interface.simulate_lap({"node":node})
        logging.info("Lap was added "+str(addTime-time)+"ms late")

    def getConnectedSeats(self):
        connectedSeats = []
        for i in range(0,len(self.flowStateMeta)):
            sto = self.seatLastMessageTimes[i]
            
            if(monotonic()-sto<UPDATE_TIMEOUT):
                connectedSeats.append(True)
            else:
                connectedSeats.append(False)
                self.flowState['states'][i] = self.blankState
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
        openSeat = 0

        a = []
        found = False
        for i in range(0,len(self.flowStateMeta)):
            sto = self.seatLastMessageTimes[i]
            a.append(monotonic()-sto)
            if(monotonic()-sto>UPDATE_TIMEOUT):
                openSeat = i
                found = True
                break
        if(found):
            logging.info("found open seat: "+str(openSeat))
        else:
            logging.info("no open seats available")
            openSeat = -1
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
                        logging.info("Callsign: "+str(foundPilot.callsign))
                        foundPilot.callsign = data["steamName"]
                        break
        if(foundPilot==None):
            logging.info("creating new pilot "+str(data["steamName"]))
            foundPilot = self.rhapi.db.pilot_add(name=data["steamName"], callsign=data["steamName"], phonetic=None, team=None, color=None)
            logging.info(str(foundPilot.id))
            self.rhapi.db.pilot_alter(pilot_id=foundPilot.id, attributes={STEAM_ID:data["steamId"]})
        seat = self.findOpenSeat()
        self.rhapi.ui.socket_send("fs_join_success", {"id":foundPilot.id, "seat":seat})
        logging.info("done")

    def handleSpectate(self):
        #echo the flow state
        self.rhapi.ui.socket_send("fs", self.flowState)

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

        self.seatLastMessageTimes[seat] = stateArrivalTime

        #let's keep track of when this player was last updated
        self.flowStateMeta[seat]["lastUpdateTime"] = stateArrivalTime

        #handle tasks that need to run every time we get a client update
        self.handleAutoRun()
        self.handleEarlyFinish()
        
    def setRSSI(self, seat, value):
        interface = self.rhapi.interface
        nodes = interface.seats
        nodes[seat].current_rssi = value
    
    def setClientSettings(self):
        logging.info("setClientSettings")
        #TO-DO get rid of async state
        serverSettings = {"track":self.track, "serverTickRate": self.serverTickRate, "clientTickRate": self.clientTickRate, "jitterDampening": (100.0-float(self.clientJitterCompensation))/100.0, "asyncState": True}
        self.rhapi.ui.socket_broadcast("fs_server_settings", serverSettings)

    def apply(self, args):
        logging.info("apply")
        interface = self.rhapi.interface
        nodes = interface.seats
        self.serverTickRate = int(self.rhapi.db.option(SERVER_TICK_RATE_INPUT))
        self.clientTickRate = int(self.rhapi.db.option(CLIENT_TICK_RATE_INPUT))
        self.clientJitterCompensation = int(self.rhapi.db.option(CLIENT_JITTER_COMP_INPUT))
        self.track = self.rhapi.db.option(TRACK_INPUT)
        self.setClientSettings()

