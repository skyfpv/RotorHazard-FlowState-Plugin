import logging
import RHUtils
import json
import requests
from eventmanager import Evt
import Config
from RHUI import UIField, UIFieldType, UIFieldSelectOption
import struct
import time

logger = logging.getLogger(__name__)

PANEL_NAME = "FlowStatePanel"
SERVER_TICK_RATE_INPUT = "ServerTickRateInput"
ASYNC_STATE_INPUT = "AsyncStateInput"
AUTO_RUN_INPUT = "AutoRun"
CLIENT_TICK_RATE_INPUT = "ClientTickRateInput"
CLIENT_JITTER_COMP_INPUT = "ClientJitterCompInput"
TRACK_INPUT = "TrackInput"

def initialize(rhapi):
    logging.info("--------------INITIALIZE FLOW STATE--------------")
    RH = FSManager(rhapi)
    rhapi.ui.register_panel(PANEL_NAME, 'FlowState', 'run', order=0)

    serverTickRateField = UIField(name = SERVER_TICK_RATE_INPUT, label = 'Server Tick Rate', field_type = UIFieldType.BASIC_INT, value = 15)
    rhapi.fields.register_option(serverTickRateField, PANEL_NAME)

    clientTickRateField = UIField(name = CLIENT_TICK_RATE_INPUT, label = 'Client Tick Rate (ignored if Async State is enabled)', field_type = UIFieldType.BASIC_INT, value = 20)
    rhapi.fields.register_option(clientTickRateField, PANEL_NAME)

    jitterCompField = UIField(name = CLIENT_JITTER_COMP_INPUT, label = 'Client Smoothing (0-100)', field_type = UIFieldType.BASIC_INT, value = 50)
    rhapi.fields.register_option(jitterCompField, PANEL_NAME)

    trackField = UIField(name = TRACK_INPUT, label = 'Track', field_type = UIFieldType.TEXT, value = "The Shrine")
    rhapi.fields.register_option(trackField, PANEL_NAME)

    asyncStateField = UIField(name = ASYNC_STATE_INPUT, label = 'Async State', field_type = UIFieldType.CHECKBOX, value = "1")
    rhapi.fields.register_option(asyncStateField, PANEL_NAME)

    autRun = UIField(name = AUTO_RUN_INPUT, label = 'Auto Run Next Heat', field_type = UIFieldType.CHECKBOX, value = "0")
    rhapi.fields.register_option(autRun, PANEL_NAME)

    rhapi.ui.register_quickbutton(PANEL_NAME, 'apply', 'Apply', RH.apply)
    
    logging.info("--------------FLOW STATE INITIALIZED--------------")

class FSManager():
    def __init__(self, rhapi):
        self.rhapi = rhapi

        #websocket listeners
        self.rhapi.ui.socket_listen("fs_set_state", self.setPlayerState)
        self.rhapi.ui.socket_listen("fs_get_settings", self.setClientSettings)
        self.rhapi.ui.socket_listen("fs_player_join", self.handlePlayerJoin)

        #main game state that will be distributed to all players as well as updated by them
        blankState = {"seat": -1, "position":[0,0,0], "orientation":[0,0,0], "rssi":0}
        self.flowState = {"time":0.0,"states":[blankState,blankState,blankState,blankState,blankState,blankState,blankState,blankState]}
        blankMeta = {"lastUpdateTime":0.0}
        self.flowStateMeta = [blankMeta,blankMeta,blankMeta,blankMeta,blankMeta,blankMeta,blankMeta,blankMeta]

        self.seatTimeout = [0,0,0,0,0,0,0,0]

        #load our server settings or fallback to default value
        logging.info("Flowstate: Loading server settings")
        serverTickRate = self.rhapi.db.option(SERVER_TICK_RATE_INPUT)
        clientTickRate = self.rhapi.db.option(CLIENT_TICK_RATE_INPUT)
        clientJitterCompensation = self.rhapi.db.option(CLIENT_JITTER_COMP_INPUT)
        asyncState = self.rhapi.db.option(ASYNC_STATE_INPUT)
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
        if(asyncState==None):
            logging.info("Flowstate: loading default async state")
            asyncState = "1"
        if(track==None):
            logging.info("Flowstate: loading default async state")
            track = "The Shrine"

        logging.info("Flowstate: fs object")
        self.serverTickRate = int(serverTickRate)
        self.clientTickRate = int(clientTickRate)
        self.clientJitterCompensation = int(clientJitterCompensation)
        self.asyncState = bool(int(asyncState))
        self.track = track

        self.lastTick = time.time()

    #def stateHandler(self, data):
    #    logging.info("Flowstate got a sim lap!")
    #    logging.info(str(data))

    def handleDiscardLaps(self):
        logging.info("handling stage_race")
        

    def handleAutoRun(self):
        if(self.rhapi.db.option(AUTO_RUN_INPUT)=="1"):
            if(self.rhapi.race.status==2):
                if(self.rhapi.race.scheduled==None):
                    logging.info("race status: "+str(self.rhapi.race.status))
                    self.rhapi.race.clear()
                    self.rhapi.race.schedule(11)

    def findOpenSeat(self):
        logging.info("findOpenSeat")
        openSeat = 0

        a = []
        for i in range(0,len(self.flowStateMeta)):
            sto = self.seatTimeout[i]
            a.append(time.time()-sto)
            if(time.time()-sto>10):
                openSeat = i
        logging.info(str(a))
        logging.info("found open seat: "+str(openSeat))
        return openSeat

    def handlePlayerJoin(self):
        logging.info("handlePlayerJoin")
        seat = self.findOpenSeat()
        self.rhapi.ui.socket_send("fs_set_seat", {"seat":seat})

    def setPlayerState(self, data):
        self.handleAutoRun()
        #logging.info(str(data))
        seat = data["seat"]
        rssi = data["rssi"]

        #let's keep track of when this player was last updated
        self.flowStateMeta[seat]["lastUpdateTime"] = time.time()

        self.seatTimeout[seat] = time.time()

        self.setRSSI(seat, rssi)
        self.flowState["states"][seat] = data
        self.flowState["time"] = time.time()
        
        #if we are updating clients asynchronously
        if(self.asyncState):
            #echo the flow state
            self.rhapi.ui.socket_send("fs", self.flowState)
        else: #otherwise
            #check if it's time to send a new update
            elapsed = time.time()-self.lastTick
            if(elapsed>(1.0/self.serverTickRate)):
                #update all the clients
                self.rhapi.ui.socket_broadcast("fs", self.flowState)
                self.lastTick = time.time()
                #logging.info(self.flowState["time"])
        
    def setRSSI(self, seat, value):
        interface = self.rhapi.interface
        nodes = interface.seats
        nodes[seat].current_rssi = value
    
    def setClientSettings(self):
        logging.info("setClientSettings")
        serverSettings = {"track":self.track, "serverTickRate": self.serverTickRate, "clientTickRate": self.clientTickRate, "jitterDampening": (100.0-float(self.clientJitterCompensation))/100.0, "asyncState": bool(int(self.asyncState))}
        self.rhapi.ui.socket_broadcast("fs_server_settings", serverSettings)

    def apply(self, args):
        logging.info("apply")
        interface = self.rhapi.interface
        nodes = interface.seats
        self.serverTickRate = int(self.rhapi.db.option(SERVER_TICK_RATE_INPUT))
        self.clientTickRate = int(self.rhapi.db.option(CLIENT_TICK_RATE_INPUT))
        self.asyncState = self.rhapi.db.option(ASYNC_STATE_INPUT)
        self.clientJitterCompensation = int(self.rhapi.db.option(CLIENT_JITTER_COMP_INPUT))
        self.track = self.rhapi.db.option(TRACK_INPUT)
        self.setClientSettings()

    def packData(self, data):
        # Your list of floating-point numbers
        

        # Flatten the list into a single list of floats
        flat_data = [num for row in data for num in row]

        # Pack the floats as binary data (assuming little-endian encoding)
        binary_data = struct.pack('<{}f'.format(len(flat_data)), *flat_data)

        # Convert the binary data to an integer
        encoded_integer = int.from_bytes(binary_data, byteorder='big')  # You can use 'little' if you prefer little-endian encoding

        return encoded_integer
