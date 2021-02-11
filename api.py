# -*- coding: utf-8 -*-

"""API handler"""

#  Copyright (c) 2013 Bernd Kreuss <prof7bit@gmail.com>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

import sys
PY_VERSION = sys.version_info

if PY_VERSION < (2, 7):
    print("Sorry, minimal Python version is 2.7, you have: %d.%d"
          % (PY_VERSION.major, PY_VERSION.minor))
    sys.exit(1)

from ConfigParser import SafeConfigParser
import base64
import contextlib
from Crypto.Cipher import AES
import getpass
import gzip
import hashlib
import inspect
import io
import json
import logging
import time
import traceback
import threading
from urllib2 import Request as URLRequest
from urllib2 import urlopen, HTTPError
import weakref

input = raw_input

FORCE_PROTOCOL = ""
FORCE_NO_FULLDEPTH = False
FORCE_NO_DEPTH = False
FORCE_NO_LAG = False
FORCE_NO_HISTORY = False
FORCE_HTTP_API = False
FORCE_NO_HTTP_API = False

USER_AGENT = "PyTrader"


def http_request(url, post=None, headers=None):
    """request data from the HTTP API, returns the response a string. If a
    http error occurs it will *not* raise an exception, instead it will
    return the content of the error document. This is because we get
    sent 5xx http status codes even if application level errors occur
    (such as canceling the same order twice or things like that) and the
    real error message will be in the json that is returned, so the return
    document is always much more interesting than the http status code."""

    def read_gzipped(response):
        """read data from the response object,
        unzip if necessary, return text string"""
        if response.info().get('Content-Encoding') == 'gzip':
            with io.BytesIO(response.read()) as buf:
                with gzip.GzipFile(fileobj=buf) as unzipped:
                    data = unzipped.read()
        else:
            data = response.read()
        return data

    if not headers:
        headers = {}
    request = URLRequest(url, post, headers)
    request.add_header('Accept-encoding', 'gzip')
    request.add_header('User-Agent', USER_AGENT)
    data = ""
    try:
        with contextlib.closing(urlopen(request, post)) as res:
            data = read_gzipped(res)
    except HTTPError as err:
        data = read_gzipped(err)
    except Exception as exc:
        logging.debug("### exception in http_request: %s" % exc)

    return data

def start_thread(thread_func, name=None):
    """start a new thread to execute the supplied function"""
    thread = threading.Thread(None, thread_func)
    thread.daemon = True
    thread.start()
    if name:
        thread.name = name
    return thread

def pretty_format(something):
    """pretty-format a nested dict or list for debugging purposes.
    If it happens to be a valid json string then it will be parsed first"""
    try:
        return pretty_format(json.loads(something))
    except Exception:
        try:
            return json.dumps(something, indent=5)
        except Exception:
            return str(something)


class ApiConfig(SafeConfigParser):
    """return a config parser object with default values. If you need to run
    more Api() objects at the same time you will also need to give each of them
    them a separate ApiConfig() object. For this reason it takes a filename
    in its constructor for the ini file, you can have separate configurations
    for separate Api() instances"""

    _DEFAULTS = [["api", "base_currency", "XETH"],
                 ["api", "quote_currency", "XXBT"],
                 ["api", "use_ssl", "True"],
                 ["api", "use_plain_old_websocket", "False"],
                 ["api", "use_http_api", "True"],
                 ["api", "use_tonce", "True"],
                 ["api", "load_fulldepth", "True"],
                 ["api", "load_history", "True"],
                 ["api", "history_timeframe", "15"],
                 ["api", "secret_key", ""],
                 ["api", "secret_secret", ""]]

    def __init__(self, filename):
        self.filename = filename
        SafeConfigParser.__init__(self)
        self.load()
        self.init_defaults(self._DEFAULTS)
        # upgrade from deprecated "currency" to "quote_currency"
        # todo: remove this piece of code again in a few months
        if self.has_option("api", "currency"):
            self.set("api", "quote_currency", self.get_string("api", "currency"))
            self.remove_option("api", "currency")
            self.save()

    def init_defaults(self, defaults):
        """add the missing default values, default is a list of defaults"""
        for (sect, opt, default) in defaults:
            self._default(sect, opt, default)

    def save(self):
        """save the config to the .ini file"""
        with open(self.filename, 'wb') as configfile:
            self.write(configfile)

    def load(self):
        """(re)load the onfig from the .ini file"""
        self.read(self.filename)

    def get_safe(self, sect, opt):
        """get value without throwing exception."""
        try:
            return self.get(sect, opt)

        except:
            for (dsect, dopt, default) in self._DEFAULTS:
                if dsect == sect and dopt == opt:
                    self._default(sect, opt, default)
                    return default
            return ""

    def get_bool(self, sect, opt):
        """get boolean value from config"""
        return self.get_safe(sect, opt) == "True"

    def get_string(self, sect, opt):
        """get string value from config"""
        return self.get_safe(sect, opt)

    def get_int(self, sect, opt):
        """get int value from config"""
        vstr = self.get_safe(sect, opt)
        try:
            return int(vstr)
        except ValueError:
            return 0

    def get_float(self, sect, opt):
        """get int value from config"""
        vstr = self.get_safe(sect, opt)
        try:
            return float(vstr)
        except ValueError:
            return 0.0

    def _default(self, section, option, default):
        """create a default option if it does not yet exist"""
        if not self.has_section(section):
            self.add_section(section)
        if not self.has_option(section, option):
            self.set(section, option, default)
            self.save()


class Signal():
    """callback functions (so called slots) can be connected to a signal and
    will be called when the signal is called (Signal implements __call__).
    The slots receive two arguments: the sender of the signal and a custom
    data object. Two different threads won't be allowed to send signals at the
    same time application-wide, concurrent threads will have to wait until
    the lock is releaesed again. The lock allows recursive reentry of the same
    thread to avoid deadlocks when a slot wants to send a signal itself."""

    _lock = threading.RLock()
    signal_error = None

    def __init__(self):
        self._functions = weakref.WeakSet()
        self._methods = weakref.WeakKeyDictionary()

        # the Signal class itself has a static member signal_error where it
        # will send tracebacks of exceptions that might happen. Here we
        # initialize it if it does not exist already
        if not Signal.signal_error:
            Signal.signal_error = 1
            Signal.signal_error = Signal()

    def connect(self, slot):
        """connect a slot to this signal. The parameter slot can be a funtion
        that takes exactly 2 arguments or a method that takes self plus 2 more
        arguments, or it can even be even another signal. the first argument
        is a reference to the sender of the signal and the second argument is
        the payload. The payload can be anything, it totally depends on the
        sender and type of the signal."""
        if inspect.ismethod(slot):
            instance = slot.__self__
            function = slot.__func__
            if instance not in self._methods:
                self._methods[instance] = set()
            if function not in self._methods[instance]:
                self._methods[instance].add(function)
        else:
            if slot not in self._functions:
                self._functions.add(slot)

    def __call__(self, sender, data, error_signal_on_error=True):
        """dispatch signal to all connected slots. This is a synchronuos
        operation, It will not return before all slots have been called.
        Also only exactly one thread is allowed to emit signals at any time,
        all other threads that try to emit *any* signal anywhere in the
        application at the same time will be blocked until the lock is released
        again. The lock will allow recursive reentry of the seme thread, this
        means a slot can itself emit other signals before it returns (or
        signals can be directly connected to other signals) without problems.
        If a slot raises an exception a traceback will be sent to the static
        Signal.signal_error() or to logging.critical()"""
        with self._lock:
            sent = False
            errors = []
            for func in self._functions:
                try:
                    func(sender, data)
                    sent = True

                except:
                    errors.append(traceback.format_exc())

            for instance, functions in self._methods.items():
                for func in functions:
                    try:
                        func(instance, sender, data)
                        sent = True

                    except:
                        errors.append(traceback.format_exc())

            for error in errors:
                if error_signal_on_error:
                    Signal.signal_error(self, (error), False)
                else:
                    logging.critical(error)

            return sent


class BaseObject():
    """This base class only exists because of the debug() method that is used
    in many of the PyTrader objects to send debug output to the signal_debug."""

    def __init__(self):
        self.signal_debug = Signal()

    def debug(self, *args):
        """send a string composed of all *args to all slots that
        are connected to signal_debug or send it to the logger if
        none are connected"""
        msg = " ".join([unicode(x) for x in args])
        if not self.signal_debug(self, (msg)):
            logging.debug(msg)


class Timer(Signal):
    """a simple timer (used for stuff like keepalive)."""

    def __init__(self, interval, one_shot=False):
        """create a new timer, interval is in seconds"""
        Signal.__init__(self)
        self._one_shot = one_shot
        self._canceled = False
        self._interval = interval
        self._timer = None
        self._start()

    def _fire(self):
        """fire the signal and restart it"""
        if not self._canceled:
            self.__call__(self, None)
            if not (self._canceled or self._one_shot):
                self._start()

    def _start(self):
        """start the timer"""
        self._timer = threading.Timer(self._interval, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self):
        """cancel the timer"""
        self._canceled = True
        self._timer.cancel()
        self._timer = None


class Secret:
    """Manage the API secret. This class has methods to decrypt the
    entries in the ini file and it also provides a method to create these
    entries. The methods encrypt() and decrypt() will block and ask
    questions on the command line, they are called outside the curses
    environment (yes, its a quick and dirty hack but it works for now)."""

    S_OK = 0
    S_FAIL = 1
    S_NO_SECRET = 2
    S_FAIL_FATAL = 3

    def __init__(self, config):
        """initialize the instance"""
        self.config = config
        self.key = ""
        self.secret = ""

        self.password_from_commandline_option = None

    def decrypt(self, password):
        """decrypt "secret_secret" from the ini file with the given password.
        This will return false if decryption did not seem to be successful.
        After this menthod succeeded the application can access the secret"""

        key = self.config.get_string("api", "secret_key")
        sec = self.config.get_string("api", "secret_secret")
        if sec == "" or key == "":
            return self.S_NO_SECRET

        hashed_pass = hashlib.sha512(password.encode("utf-8")).digest()
        crypt_key = hashed_pass[:32]
        crypt_ini = hashed_pass[-16:]
        aes = AES.new(crypt_key, AES.MODE_OFB, crypt_ini)
        try:
            encrypted_secret = base64.b64decode(sec.strip().encode("ascii"))
            self.secret = aes.decrypt(encrypted_secret).strip()
            self.key = key.strip()
        except ValueError:
            return self.S_FAIL

        # now test if we now have something plausible
        try:
            print("testing secret...")
            # is it plain ascii? (if not this will raise exception)
            # dummy = self.secret.decode("ascii")
            # can it be decoded? correct size afterwards?
            if len(base64.b64decode(self.secret)) != 64:
                raise Exception("Decrypted secret has wrong size")
            if not self.secret:
                raise Exception("Unable to decrypt secret")

            print("testing key...")
            # key must be only hex digits and have the right size
            # hex_key = self.key.replace("-", "").encode("ascii")
            # if len(binascii.unhexlify(hex_key)) != 16:
            #     raise Exception("key has wrong size")
            if not self.key:
                raise Exception("Unable to decrypt key")

            print("OK")
            return self.S_OK

        except Exception as exc:
            # this key and secret do not work
            self.secret = ""
            self.key = ""
            print("### Error occurred while testing the decrypted secret:")
            print("    '%s'" % exc)
            print("    This does not seem to be a valid API secret")
            return self.S_FAIL

    def prompt_decrypt(self):
        """ask the user for password on the command line
        and then try to decrypt the secret."""
        if self.know_secret():
            return self.S_OK

        key = self.config.get_string("api", "secret_key")
        sec = self.config.get_string("api", "secret_secret")
        if sec == "" or key == "":
            return self.S_NO_SECRET

        if self.password_from_commandline_option:
            password = self.password_from_commandline_option
        else:
            password = getpass.getpass("enter passphrase for secret: ")

        result = self.decrypt(password)
        if result != self.S_OK:
            print("")
            print("secret could not be decrypted")
            answer = input("press any key to continue anyways "
                           + "(trading disabled) or 'q' to quit: ")
            if answer == "q":
                result = self.S_FAIL_FATAL
            else:
                result = self.S_NO_SECRET
        return result

    def prompt_encrypt(self):
        """ask for key, secret and password on the command line,
        then encrypt the secret and store it in the ini file."""
        print("Please copy/paste key and secret from exchange and")
        print("then provide a password to encrypt them.")
        print("")

        key = input("             key: ").strip()
        secret = input("          secret: ").strip()
        while True:
            password1 = getpass.getpass("        password: ").strip()
            if password1 == "":
                print("aborting")
                return
            password2 = getpass.getpass("password (again): ").strip()
            if password1 != password2:
                print("you had a typo in the password. try again...")
            else:
                break

        hashed_pass = hashlib.sha512(password1.encode("utf-8")).digest()
        crypt_key = hashed_pass[:32]
        crypt_ini = hashed_pass[-16:]
        aes = AES.new(crypt_key, AES.MODE_OFB, crypt_ini)

        # since the secret is a base64 string we can just just pad it with
        # spaces which can easily be stripped again after decryping
        print(len(secret))
        secret += " " * (16 - len(secret) % 16)
        print(len(secret))
        secret = base64.b64encode(aes.encrypt(secret)).decode("ascii")

        self.config.set("api", "secret_key", key)
        self.config.set("api", "secret_secret", secret)
        self.config.save()

        print("encrypted secret has been saved in %s" % self.config.filename)

    def know_secret(self):
        """do we know the secret key? The application must be able to work
        without secret and then just don't do any account related stuff"""
        return(self.secret != "") and (self.key != "")


class OHLCV():
    """represents a chart candle. tim is POSIX timestamp of open time,
    prices and volume are integers like in the other parts of the API"""

    def __init__(self, tim, opn, hig, low, cls, vol):
        self.tim = tim
        self.opn = opn
        self.hig = hig
        self.low = low
        self.cls = cls
        self.vol = vol

    def update(self, price, volume):
        """update high, low and close values and add to volume"""
        if price > self.hig:
            self.hig = price
        if price < self.low:
            self.low = price
        self.cls = price
        self.vol += volume


class History(BaseObject):
    """represents the trading history"""

    def __init__(self, api, timeframe):
        BaseObject.__init__(self)

        self.signal_fullhistory_processed = Signal()
        self.signal_changed = Signal()

        self.api = api
        self.candles = []
        self.timeframe = timeframe

        self.ready_history = False

        api.signal_trade.connect(self.slot_trade)
        api.signal_fullhistory.connect(self.slot_fullhistory)

    def add_candle(self, candle):
        """add a new candle to the history"""
        self._add_candle(candle)
        self.signal_changed(self, (self.length()))

    def slot_trade(self, dummy_sender, data):
        """slot for api.signal_trade"""
        (date, price, volume, dummy_typ, own) = data
        if not own:
            time_round = int(date / self.timeframe) * self.timeframe
            candle = self.last_candle()
            if candle:
                if candle.tim == time_round:
                    candle.update(price, volume)
                    self.signal_changed(self, (1))
                else:
                    self.debug("### opening new candle")
                    self.add_candle(OHLCV(
                        time_round, price, price, price, price, volume))
            else:
                self.add_candle(OHLCV(
                    time_round, price, price, price, price, volume))

    def _add_candle(self, candle):
        """add a new candle to the history but don't fire signal_changed"""
        self.candles.insert(0, candle)

    def slot_fullhistory(self, dummy_sender, data):
        """process the result of the fullhistory request"""
        (history) = data

        if not len(history):
            self.debug("### history download was empty")
            return

        def get_time_round(date):
            """round timestamp to current candle timeframe"""
            return int(date / self.timeframe) * self.timeframe

        # remove existing recent candle(s) if any, we will create them fresh
        date_begin = get_time_round(history[0]["date"])
        while len(self.candles) and self.candles[0].tim >= date_begin:
            self.candles.pop(0)

        new_candle = OHLCV(0, 0, 0, 0, 0, 0)  # this is a dummy, not actually inserted
        count_added = 0
        for trade in history:
            date = trade["date"]
            price = trade["price"]
            volume = trade["amount"]
            time_round = get_time_round(date)
            if time_round > new_candle.tim:
                if new_candle.tim > 0:
                    self._add_candle(new_candle)
                    count_added += 1
                new_candle = OHLCV(time_round, price, price, price, price, volume)
            new_candle.update(price, volume)

        # insert current (incomplete) candle
        self._add_candle(new_candle)
        count_added += 1
        # self.debug("### got %d updated candle(s)" % count_added)
        self.ready_history = True
        self.signal_fullhistory_processed(self, None)
        self.signal_changed(self, (self.length()))

    def last_candle(self):
        """return the last (current) candle or None if empty"""
        if self.length() > 0:
            return self.candles[0]
        else:
            return None

    def length(self):
        """return the number of candles in the history"""
        return len(self.candles)


class Api(BaseObject):
    """represents the API of the exchange. An Instance of this
    class will connect to the streaming socket.io API, receive live
    events, it will emit signals you can hook into for all events,
    it has methods to buy and sell"""

    def __init__(self, secret, config):
        """initialize the API but do not yet connect to it."""
        BaseObject.__init__(self)

        self.signal_depth = Signal()
        self.signal_trade = Signal()
        self.signal_ticker = Signal()
        self.signal_fulldepth = Signal()
        self.signal_fullhistory = Signal()
        self.signal_wallet = Signal()
        self.signal_userorder = Signal()
        self.signal_orderlag = Signal()
        self.signal_disconnected = Signal()  # socket connection lost
        self.signal_ready = Signal()  # connected and fully initialized

        self.signal_order_too_fast = Signal()  # don't use that

        self.strategies = weakref.WeakValueDictionary()

        # the following are not fired by the api itself but by the
        # application controlling it to pass some of its events
        self.signal_keypress = Signal()
        self.signal_strategy_unload = Signal()

        # self._idkey = ""
        self.wallet = {}
        self.trade_fee = 0  # percent (float, for example 0.6 means 0.6%)
        self.monthly_volume = 0  # variable currency per exchange
        self.order_lag = 0  # microseconds
        self.socket_lag = 0  # microseconds
        self.last_tid = 0
        self.count_submitted = 0  # number of submitted orders not yet acked
        self.msg = {}  # the incoming message that is currently processed

        # the following will be set to true once the information
        # has been received after connect, once all thes flags are
        # true it will emit the signal_connected.
        # self.ready_idkey = False
        self.ready_info = False
        self._was_disconnected = True

        self.config = config
        self.curr_base = config.get_string("api", "base_currency")
        self.curr_quote = config.get_string("api", "quote_currency")

        self.currency = self.curr_quote  # used for monthly_volume currency

        self.exchange = config.get_string("pytrader", "exchange")

        # these are needed for conversion from/to intereger, float, string
        self.mult_quote = 1e5
        self.format_quote = "%12.5f"
        self.mult_base = 1e8
        self.format_base = "%16.8f"

        Signal.signal_error.connect(self.signal_debug)

        timeframe = 60 * config.get_int("api", "history_timeframe")
        if not timeframe:
            timeframe = 60 * 15
        self.history = History(self, timeframe)
        self.history.signal_debug.connect(self.signal_debug)

        self.orderbook = OrderBook(self)
        self.orderbook.signal_debug.connect(self.signal_debug)

        use_websocket = self.config.get_bool("api", "use_plain_old_websocket")

        if "socketio" in FORCE_PROTOCOL:
            use_websocket = False
        if "websocket" in FORCE_PROTOCOL:
            use_websocket = True

        if self.exchange == "gox":  # So obsolete...
            if use_websocket:
                from exchanges.gox import WebsocketClient
                self.client = WebsocketClient(self.curr_base, self.curr_quote, secret, config)
            else:
                from exchanges.gox import SocketIOClient
                self.client = SocketIOClient(self.curr_base, self.curr_quote, secret, config)
        elif self.exchange == "kraken":
            from exchanges.kraken import PollClient
            self.client = PollClient(self.curr_base, self.curr_quote, secret, config)
        elif self.exchange == "poloniex":
            from exchanges.poloniex import WebsocketClient
            self.client = WebsocketClient(self.curr_base, self.curr_quote, secret, config)
        else:
            raise Exception("Unsupported exchange")

        self.client.signal_debug.connect(self.signal_debug)
        self.client.signal_disconnected.connect(self.slot_disconnected)
        self.client.signal_connected.connect(self.slot_client_connected)
        self.client.signal_recv.connect(self.slot_recv)
        self.client.signal_fulldepth.connect(self.signal_fulldepth)
        self.client.signal_fullhistory.connect(self.signal_fullhistory)
        self.client.signal_ticker.connect(self.signal_ticker)

        self.timer_poll = Timer(120)
        self.timer_poll.connect(self.slot_poll)

        self.history.signal_changed.connect(self.slot_history_changed)
        self.history.signal_fullhistory_processed.connect(self.slot_fullhistory_processed)
        self.orderbook.signal_fulldepth_processed.connect(self.slot_fulldepth_processed)
        self.orderbook.signal_owns_initialized.connect(self.slot_owns_initialized)

    def start(self):
        """connect to API and start receiving events."""
        self.debug("### Starting API, trading %s%s" % (self.curr_base, self.curr_quote))
        self.client.start()

    def stop(self):
        """shutdown the client"""
        self.debug("### shutdown...")
        self.client.stop()

    def order(self, typ, price, volume):
        """place pending order. If price=0 then it will be filled at market"""
        self.count_submitted += 1
        self.client.send_order_add(typ, price, volume)

    def buy(self, price, volume):
        """new buy order, if price=0 then buy at market"""
        self.order("bid", price, volume)

    def sell(self, price, volume):
        """new sell order, if price=0 then sell at market"""
        self.order("ask", price, volume)

    def cancel(self, oid):
        """cancel order"""
        self.client.send_order_cancel(oid)

    def cancel_by_price(self, price):
        """cancel all orders at price"""
        for i in reversed(range(len(self.orderbook.owns))):
            order = self.orderbook.owns[i]
            if order.price == price:
                if order.oid != "":
                    self.cancel(order.oid)

    def cancel_by_type(self, typ=None):
        """cancel all orders of type (or all orders if typ=None)"""
        for i in reversed(range(len(self.orderbook.owns))):
            order = self.orderbook.owns[i]
            if typ is None or typ == order.typ:
                if order.oid != "":
                    self.cancel(order.oid)

    def base2float(self, int_number):
        """convert base currency values from integer to float. Base
        currency are the coins you are trading (BTC, LTC, etc). Use this method
        to convert order volumes (amount of coins) from int to float."""
        return float(int_number) / self.mult_base

    def base2str(self, int_number):
        """convert base currency values from integer to formatted string"""
        return self.format_base % (float(int_number) / self.mult_base)

    def base2int(self, float_number):
        """convert base currency values from float to integer"""
        return int(round(float_number * self.mult_base))

    def quote2float(self, int_number):
        """convert quote currency values from integer to float. Quote
        currency is the currency used to quote prices (USD, EUR, etc), use this
        method to convert the prices of orders, bid or ask from int to float."""
        return float(int_number) / self.mult_quote

    def quote2str(self, int_number):
        """convert quote currency values from integer to formatted string"""
        return self.format_quote % (float(int_number) / self.mult_quote)

    def quote2int(self, float_number):
        """convert quote currency values from float to integer"""
        return int(round(float_number * self.mult_quote))

    def check_connect_ready(self):
        """check if everything that is needed has been downloaded
        and emit the connect signal if everything is ready"""
        need_no_account = not self.client.secret.know_secret()
        need_no_depth = not self.config.get_bool("api", "load_fulldepth")
        need_no_history = not self.config.get_bool("api", "load_history")
        need_no_depth = need_no_depth or FORCE_NO_FULLDEPTH
        need_no_history = need_no_history or FORCE_NO_HISTORY
        ready_account = self.ready_info and self.orderbook.ready_owns  # and self.ready_idkey...
        if ready_account or need_no_account:
            if self.orderbook.ready_depth or need_no_depth:
                if self.history.ready_history or need_no_history:
                    if self._was_disconnected:
                        self.signal_ready(self, None)
                        self._was_disconnected = False

    def slot_client_connected(self, _sender, _data):
        """connected to the client"""
        self.check_connect_ready()

    def slot_fulldepth_processed(self, _sender, _data):
        """connected to the orderbook"""
        self.check_connect_ready()

    def slot_fullhistory_processed(self, _sender, _data):
        """connected to the history"""
        self.check_connect_ready()

    def slot_owns_initialized(self, _sender, _data):
        """connected to the orderbook"""
        self.check_connect_ready()

    def slot_disconnected(self, _sender, _data):
        """this slot is connected to the client object, all it currently
        does is to emit a disconnected signal itself"""
        # self.ready_idkey = False
        self.ready_info = False
        self.orderbook.ready_owns = False
        self.orderbook.ready_depth = False
        self.history.ready_history = False
        self._was_disconnected = True
        self.signal_disconnected(self, None)

    def slot_recv(self, dummy_sender, data):
        """Slot for signal_recv, handle new incoming JSON message. Decode the
        JSON string into a Python object and dispatch it to the method that
        can handle it."""
        (str_json) = data
        handler = None
        if type(str_json) == dict:
            msg = str_json  # was already a dict
        else:
            msg = json.loads(str_json)
        self.msg = msg

        if "stamp" in msg:
            delay = time.time() * 1e6 - int(msg["stamp"])
            self.socket_lag = (self.socket_lag * 29 + delay) / 30

        if "op" in msg:
            try:
                msg_op = msg["op"]
                handler = getattr(self, "_on_op_" + msg_op)

            except AttributeError:
                self.debug("slot_recv() ignoring: op=%s" % msg_op)
        else:
            self.debug("slot_recv() ignoring:", msg)

        if handler:
            handler(msg)

    def slot_poll(self, _sender, _data):
        """poll stuff from http in regular intervals, not yet implemented"""
        if self.client.secret and self.client.secret.know_secret():
            # poll recent own trades
            # fixme: how do i do this, whats the api for this?
            pass

    def slot_history_changed(self, _sender, _data):
        """this is a small optimzation, if we tell the client the time
        of the last known candle then it won't fetch full history next time"""
        last_candle = self.history.last_candle()
        if last_candle:
            self.client.history_last_candle = last_candle.tim

    def _on_op_error(self, msg):
        """handle error mesages (op:error)"""
        self.debug("### _on_op_error()", msg)

    def _on_op_subscribe(self, msg):
        """handle subscribe messages (op:subscribe)"""
        self.debug("### subscribed channel", msg["channel"])

    def _on_op_ticker(self, msg):
        """handle incoming ticker message"""
        msg = msg["ticker"]

        bid = msg["bid"]
        ask = msg["ask"]

        # self.debug(" tick: %s %s" % (bid, ask))
        self.signal_ticker(self, (bid, ask))

    def _on_op_depth(self, msg):
        """handle incoming depth message"""
        msg = msg["depth"]
        # if msg["currency"] != self.curr_quote:
        #     return
        # if msg["base"] != self.curr_base:
        #     return
        typ = msg["type"]
        price = msg["price"]
        volume = msg["volume"]
        # timestamp = msg["timestamp"]
        # total_volume = msg["total_volume"]

        # delay = time.time() - timestamp
        # self.debug("depth: %s: %.8f @ %.8f total: %.8f (age: %0.4f s)" % (
        #     typ,
        #     volume,
        #     price,
        #     price * volume,
        #     delay / 1e6
        # ))
        self.signal_depth(self, (typ, price, volume))  # , total_volume))

    def _on_op_trade(self, msg):
        """handle incoming trade message"""
        # if msg["trade"]["price_currency"] != self.curr_quote:
        #     return
        # if msg["trade"]["base"] != self.curr_base:
        #     return
        # else:
        #     own = True
        trade = msg['trade']
        typ = trade["type"]
        price = trade["price"]
        volume = trade["amount"]
        timestamp = int(trade["timestamp"])

        # if own:
        #     self.debug("trade: %s: %s @ %s (own order filled)" % (
        #         typ,
        #         volume,
        #         price
        #     ))
        #     # send another private/info request because the fee might have
        #     # changed. We request it a minute later because the server
        #     # seems to need some time until the new values are available.
        #     # self.client.request_info_later(60)
        # else:
        self.debug("trade: %s: %s @ %s" % (
            typ,
            volume,
            price
        ))

        self.signal_trade(self, (timestamp, price, volume, typ, False))  # own))

    def _on_op_chat(self, msg):
        """trollbox messages"""
        msg = msg['msg']
        self.debug("[c]%s %s[%s]: %s" % (
            msg['type'] if msg['type'] != 'trollboxMessage' else ' >',
            msg['user'],
            msg['rep'],
            msg['msg']
        ))

    def _on_op_result(self, msg):
        """handle result of authenticated API call (op:result, id:xxxxxx)"""
        result = msg["result"]
        reqid = msg["id"]

        # if reqid == "idkey":
        #     self.debug("### got key, subscribing to account messages")
        #     self._idkey = result
        #     self.client.on_idkey_received(result)
        #     self.ready_idkey = True
        #     self.check_connect_ready()

        if reqid == "orders":
            # self.debug("### got own order list")
            # self.count_submitted = 0
            self.orderbook.init_own(result)
            # self.debug("### have %d own orders for %s/%s" % (len(self.orderbook.owns), self.curr_base, self.curr_quote))

        elif reqid == "info":
            # self.debug("### got account info")
            self.wallet = {}
            for currency in result:
                self.wallet[currency] = float(result[currency])

            # ## Old Gox shit
            # wallet = result["Wallets"]
            # self.monthly_volume = int(result["Monthly_Volume"]["value_int"])
            # self.trade_fee = float(result["Trade_Fee"])
            # for currency in wallet:
            #     self.wallet[currency] = int(
            #         wallet[currency]["Balance"]["value_int"])

            self.signal_wallet(self, None)
            self.ready_info = True

            if self.client._wait_for_next_info:
                self.client._wait_for_next_info = False

            self.check_connect_ready()

        elif reqid == "volume":
            self.monthly_volume = result['volume']
            self.currency = result['currency']
            self.trade_fee = result['fee']

        elif reqid == "order_lag":
            lag_usec = result["lag"]
            lag_text = result["lag_text"]
            # self.debug("### got order lag: %s" % lag_text)
            self.order_lag = lag_usec
            self.signal_orderlag(self, (lag_usec, lag_text))

        elif "order_add:" in reqid:
            # order/add has been acked and we got an oid, now we can already
            # insert a pending order into the owns list (it will be pending
            # for a while when the server is busy but the most important thing
            # is that we have the order-id already).
            parts = reqid.split(":")
            typ = parts[1]
            price = float(parts[2])
            volume = float(parts[3])
            oid = result
            self.debug("### got ack for order/add:", typ, price, volume, oid)
            self.count_submitted -= 1
            self.orderbook.add_own(Order(price, volume, typ, oid, "pending"))

        elif "order_cancel:" in reqid:
            # cancel request has been acked but we won't remove it from our
            # own list now because it is still active on the server.
            # do nothing now, let things happen in the user_order message
            parts = reqid.split(":")
            oid = parts[1]
            self.debug("### got ack for order/cancel:", oid)

        else:
            self.debug("### _on_op_result() ignoring:", msg)

    def _on_op_private(self, msg):
        """handle op=private messages, these are the messages of the channels
        we subscribed (trade, depth, ticker) and also the per-account messages
        (user_order, wallet, own trades, etc)"""
        private = msg["private"]
        handler = None
        try:
            handler = getattr(self, "_on_op_private_" + private)
        except AttributeError:
            self.debug("### _on_op_private() ignoring: private=%s" % private)
            self.debug(pretty_format(msg))

        if handler:
            handler(msg)

    def _on_op_private_user_order(self, msg):
        """handle incoming user_order message (op=private, private=user_order)"""
        order = msg["user_order"]
        oid = order["oid"]

        # there exist 3 fundamentally different types of user_order messages,
        # they differ in the presence or absence of certain parts of the message

        if "status" in order:
            # these are limit orders or market orders (new or updated).
            #
            # we also need to check whether they belong to our own gox instance,
            # since they contain currency this is easy, we compare the currency
            # and simply ignore mesages for all unrelated currencies.
            if order["currency"] == self.curr_quote and order["base"] == self.curr_base:
                volume = order["amount"]
                typ = order["type"]
                status = order["status"]
                if "price" in order:
                    # these are limit orders (new or updated)
                    price = order["price"]
                else:
                    # these are market orders (new or updated)
                    price = 0
                self.signal_userorder(self, (price, volume, typ, oid, status))

        else:
            # these are remove messages (cancel or fill)
            # here it is a bit more expensive to check whether they belong to
            # this gox instance, they don't carry any other useful data besides
            # the order id and the remove reason but since a remove message can
            # only affect us if the oid is in the owns list already we just
            # ask the orderbook instance whether it knows about this order
            # and ignore all the ones that have unknown oid
            if self.orderbook.have_own_oid(oid):
                # they don't contain a status field either, so we make up
                # our own status string to make it more useful. It will
                # be "removed:" followed by the reason. Possible reasons are:
                # "requested", "completed_passive", "completed_active"
                # so for example a cancel would be "removed:requested"
                # and a limit order fill would be "removed:completed_passive".
                status = "removed:" + order["reason"]
                self.signal_userorder(self, (0, 0, "", oid, status))

    def _on_op_private_wallet(self, msg):
        """handle incoming wallet message (op=private, private=wallet)"""
        balance = msg["wallet"]["balance"]
        currency = balance["currency"]
        total = balance["value"]
        self.wallet[currency] = total
        self.signal_wallet(self, None)

    def _on_op_private_lag(self, msg):
        """handle the lag message"""
        self.order_lag = int(msg["lag"]["age"])
        if self.order_lag < 60000000:
            text = "%0.3f s" % (int(self.order_lag / 1000) / 1000.0)
        else:
            text = "%d s" % (int(self.order_lag / 1000000))
        self.signal_orderlag(self, (self.order_lag, text))

    def _on_op_remark(self, msg):
        """handler for op=remark messages"""

        if "success" in msg and not msg["success"]:
            if msg["message"] == "Invalid call":
                self._on_invalid_call(msg)
            elif msg["message"] == "Order not found":
                self._on_order_not_found(msg)
            elif msg["message"] == "Order amount is too low":
                self._on_order_amount_too_low(msg)
            elif "Too many orders placed" in msg["message"]:
                self._on_too_many_orders(msg)
            else:
                # we should log this, helps with debugging
                self.debug(msg)

    def _on_invalid_call(self, msg):
        """FIXME"""

        # if msg["id"] == "idkey":
        #     self.debug("### resending private/idkey")
        #     self.client.send_signed_call(
        #         "private/idkey", {}, "idkey")

        if msg["id"] == "info":
            self.debug("### resending private/info")
            self.client.send_signed_call(
                "private/info", {}, "info")

        elif msg["id"] == "orders":
            self.debug("### resending private/orders")
            self.client.send_signed_call(
                "private/orders", {}, "orders")

        # elif "order_add:" in msg["id"]:
        #     parts = msg["id"].split(":")
        #     typ = parts[1]
        #     price = float(parts[2])
        #     volume = float(parts[3])
        #     self.debug("### resending failed", msg["id"])
        #     self.client.send_order_add(typ, price, volume)

        # elif "order_cancel:" in msg["id"]:
        #     parts = msg["id"].split(":")
        #     oid = parts[1]
        #     self.debug("### resending failed", msg["id"])
        #     self.client.send_order_cancel(oid)

        else:
            self.debug("### _on_invalid_call() ignoring:", msg)

    def _on_order_not_found(self, msg):
        """this means we have sent order/cancel with non-existing oid"""
        parts = msg["id"].split(":")
        oid = parts[1]
        self.debug("### got 'Order not found' for", oid)
        # we are now going to fake a user_order message (the one we
        # obviously missed earlier) that will have the effect of
        # removing the order cleanly.
        fakemsg = {"user_order": {"oid": oid, "reason": "requested"}}
        self._on_op_private_user_order(fakemsg)

    def _on_order_amount_too_low(self, _msg):
        """we received an order_amount too low message."""
        self.debug("### Server said: 'Order amount is too low'")
        self.count_submitted -= 1

    def _on_too_many_orders(self, msg):
        """server complains too many orders were placd too fast"""
        self.debug("### Server said: '%s" % msg["message"])
        self.count_submitted -= 1
        self.signal_order_too_fast(self, msg)


class Level:
    """represents a level in the orderbook"""
    def __init__(self, price, volume):
        self.price = price
        self.volume = volume
        self.own_volume = 0

        # these fields are only used to store temporary cache values
        # in some (not all!) levels and is calculated by the OrderBook
        # on demand, do not access this, use get_total_up_to() instead!
        self._cache_total_vol = 0
        self._cache_total_vol_quote = 0

class Order:
    """represents an order"""
    def __init__(self, price, volume, typ, oid="", status=""):
        """initialize a new order object"""
        self.price = price
        self.volume = volume
        self.typ = typ
        self.oid = oid
        self.status = status

class OrderBook(BaseObject):
    """represents the orderbook. Each Gox instance has one
    instance of OrderBook to maintain the open orders. This also
    maintains a list of own orders belonging to this account"""

    def __init__(self, api):
        """create a new empty orderbook and associate it with its
        Gox instance, initialize it and connect its slots to gox"""
        BaseObject.__init__(self)
        self.api = api

        self.signal_changed = Signal()
        """orderbook state has changed
        param: None
        an update to the state of the orderbook happened, this is emitted very
        often, it happens after every depth message, after every trade and
        also after every user_order message. This signal is for example used
        in pytrader.py to repaint the user interface of the orderbook window."""

        self.signal_fulldepth_processed = Signal()
        """fulldepth download is complete
        param: None
        The orderbook (fulldepth) has been downloaded from the server.
        This happens soon after connect."""

        self.signal_owns_initialized = Signal()
        """own order list has been initialized
        param: None
        The owns list has been initialized. This happens soon after connect
        after it has downloaded the authoritative list of pending and open
        orders. This will also happen if it reinitialized after lost connection."""

        self.signal_owns_changed = Signal()
        """owns list has changed
        param: None
        an update to the owns list has happened, this can be order added,
        removed or filled, status or volume of an order changed. For specific
        changes to individual orders see the signal_own_* signals below."""

        self.signal_own_added = Signal()
        """order was added
        param: (order)
        order is a reference to the Order() instance
        This signal will be emitted whenever a new order is added to
        the owns list. Orders will initially have status "pending" and
        some time later there will be signal_own_opened when the status
        changed to open."""

        self.signal_own_removed = Signal()
        """order has been removed
        param: (order, reason)
        order is a reference to the Order() instance
        reason is a string that can have the following values:
          "requested" order was canceled
          "completed_passive" limit order was filled completely
          "completed_active" market order was filled completely
        Bots will probably be interested in this signal because this is a
        reliable way to determine that a trade has fully completed because the
        trade signal alone won't tell you whether its partial or complete"""

        self.signal_own_opened = Signal()
        """order status went to "open"
        param: (order)
        order is a reference to the Order() instance
        when the order changes from 'post-pending' to 'open' then this
        signal will be emitted. It won't be emitted for market orders because
        market orders can't have an "open" status, they never move beyond
        "executing", they just execute and emit volume and removed signals."""

        self.signal_own_volume = Signal()
        """order volume changed (partial fill)
        param: (order, voldiff)
        order is a reference to the Order() instance
        voldiff is the differenc in volume, so for a partial or a complete fill
        it would contain a negative value (integer number of satoshi) of the
        difference between now and the previous volume. This signal is always
        emitted when an order is filled or partially filled, it can be emitted
        multiple times just like the trade messages. It will be emitted for
        all types of orders. The last volume signal that finally brouhgt the
        remaining order volume down to zero will be immediately followed by
        a removed signal."""

        self.bids = []  # list of Level(), lowest ask first
        self.asks = []  # list of Level(), highest bid first
        self.owns = []  # list of Order(), unordered list

        self.bid = 0
        self.ask = 0
        self.total_bid = 0
        self.total_ask = 0

        self.ready_depth = False
        self.ready_owns = False

        self.last_change_type = None  # ("bid", "ask", None) this can be used
        self.last_change_price = 0  # for highlighting relative changes
        self.last_change_volume = 0  # of orderbook levels in pytrader.py

        self.depth_updated = '-'
        self.orders_updated = '-'

        self._valid_bid_cache = -1   # index of bid with valid _cache_total_vol
        self._valid_ask_cache = -1   # index of ask with valid _cache_total_vol

        api.signal_ticker.connect(self.slot_ticker)
        api.signal_depth.connect(self.slot_depth)
        api.signal_trade.connect(self.slot_trade)
        api.signal_userorder.connect(self.slot_user_order)
        api.signal_fulldepth.connect(self.slot_fulldepth)

    def slot_ticker(self, dummy_sender, data):
        """Slot for signal_ticker, incoming ticker message"""
        (bid, ask) = data
        self.bid = bid
        self.ask = ask
        self.last_change_type = None
        self.last_change_price = 0
        self.last_change_volume = 0
        self._repair_crossed_asks(ask)
        self._repair_crossed_bids(bid)
        self.signal_changed(self, None)

    def slot_depth(self, dummy_sender, data):
        """Slot for signal_depth, process incoming depth message"""
        (typ, price, total_vol) = data
        if self._update_book(typ, price, total_vol):
            self.signal_changed(self, None)

    def slot_trade(self, dummy_sender, data):
        """Slot for signal_trade event, process incoming trade messages.
        For trades that also affect own orders this will be called twice:
        once during the normal public trade message, affecting the public
        bids and asks and then another time with own=True to update our
        own orders list"""
        (dummy_date, price, volume, typ, own) = data
        if own:
            # nothing special to do here (yet), there will also be
            # separate user_order messages to update my owns list
            # and a copy of this trade message in the public channel
            pass
        else:
            # we update the orderbook. We could also wait for the depth
            # message but we update the orderbook immediately.
            voldiff = -volume
            if typ == "bid":  # typ=bid means an ask order was filled
                self._repair_crossed_asks(price)
                if len(self.asks):
                    if self.asks[0].price == price:
                        self.asks[0].volume -= volume
                        if self.asks[0].volume <= 0:
                            voldiff -= self.asks[0].volume
                            self.asks.pop(0)
                        self.last_change_type = "ask"  # the asks have changed
                        self.last_change_price = price
                        self.last_change_volume = voldiff
                        self._update_total_ask(voldiff)
                        self._valid_ask_cache = -1
                if len(self.asks):
                    self.ask = self.asks[0].price

            if typ == "ask":  # typ=ask means a bid order was filled
                self._repair_crossed_bids(price)
                if len(self.bids):
                    if self.bids[0].price == price:
                        self.bids[0].volume -= volume
                        if self.bids[0].volume <= 0:
                            voldiff -= self.bids[0].volume
                            self.bids.pop(0)
                        self.last_change_type = "bid"  # the bids have changed
                        self.last_change_price = price
                        self.last_change_volume = voldiff
                        self._update_total_bid(voldiff, price)
                        self._valid_bid_cache = -1
                if len(self.bids):
                    self.bid = self.bids[0].price

        self.signal_changed(self, None)

    def slot_user_order(self, dummy_sender, data):
        """Slot for signal_userorder, process incoming user_order message"""
        (price, volume, typ, oid, status) = data
        found = False
        removed = False  # was the order removed?
        opened = False  # did the order change from 'post-pending' to 'open'"?
        voldiff = 0     # did the order volume change (full or partial fill)
        if "executing" in status:
            # don't need this status at all
            return
        if "post-pending" in status:
            # don't need this status at all
            return
        if "removed" in status:
            for i in range(len(self.owns)):
                if self.owns[i].oid == oid:
                    order = self.owns[i]

                    # work around strangeness:
                    # for some reason it will send a "completed_passive"
                    # immediately followed by a "completed_active" when a
                    # market order is filled and removed. Since "completed_passive"
                    # is meant for limit orders only we will just completely
                    # IGNORE all "completed_passive" if it affects a market order,
                    # there WILL follow a "completed_active" immediately after.
                    if order.price == 0:
                        if "passive" in status:
                            # ignore it, the correct one with
                            # "active" will follow soon
                            return

                    self.debug(
                        "### removing order %s " % oid,
                        "price:", order.price,
                        "type:", order.typ)

                    # remove it from owns...
                    self.owns.pop(i)

                    # ...and update own volume cache in the bids or asks
                    self._update_level_own_volume(
                        order.typ,
                        order.price,
                        self.get_own_volume_at(order.price, order.typ)
                    )
                    removed = True
                    break
        else:
            for order in self.owns:
                if order.oid == oid:
                    found = True
                    self.debug(
                        "### updating order %s " % oid,
                        "volume:", volume,
                        "status:", status)
                    voldiff = volume - order.volume
                    opened = (order.status != "open" and status == "open")
                    order.volume = volume
                    order.status = status
                    break

            if not found:
                # This can happen if we added the order with a different
                # application or the gox server sent the user_order message
                # before the reply to "order/add" (this can happen because
                # actually there is no guarantee which one arrives first).
                # We will treat this like a reply to "order/add"
                self.add_own(Order(price, volume, typ, oid, status))

                # The add_own() method has handled everything that was needed
                # for new orders and also emitted all signals already, we
                # can immediately return here because the job is done.
                return

            # update level own volume cache
            self._update_level_own_volume(
                typ, price, self.get_own_volume_at(price, typ))

        # We try to help the strategy with tracking the orders as good
        # as we can by sending different signals for different events.
        if removed:
            reason = self.api.msg["user_order"]["reason"]
            self.signal_own_removed(self, (order, reason))
        if opened:
            self.signal_own_opened(self, (order))
        if voldiff:
            self.signal_own_volume(self, (order, voldiff))
        self.signal_changed(self, None)
        self.signal_owns_changed(self, None)

    def slot_fulldepth(self, dummy_sender, data):
        """Slot for signal_fulldepth, process received fulldepth data.
        This will clear the book and then re-initialize it from scratch."""
        (depth) = data
        # self.debug("### got full depth, updating orderbook...")
        self.bids = []
        self.asks = []
        self.total_ask = 0
        self.total_bid = 0
        if "error" in depth and depth['error']:
            self.debug("### ", depth["error"])
            return
        for order in depth["data"]["asks"]:
            price = order["price"]
            volume = order["amount"]
            self._update_total_ask(volume)
            self.asks.append(Level(price, volume))
        for order in depth["data"]["bids"]:
            price = order["price"]
            volume = order["amount"]
            self._update_total_bid(volume, price)
            self.bids.insert(0, Level(price, volume))

        # update own volume cache
        for order in self.owns:
            self._update_level_own_volume(
                order.typ, order.price, self.get_own_volume_at(order.price, order.typ))

        if len(self.bids):
            self.bid = self.bids[0].price
        if len(self.asks):
            self.ask = self.asks[0].price

        self._valid_ask_cache = -1
        self._valid_bid_cache = -1
        self.ready_depth = True
        self.depth_updated = time.strftime("%Y-%m-%d %H:%M:%S")
        self.signal_fulldepth_processed(self, None)
        self.signal_changed(self, None)

    def _repair_crossed_bids(self, bid):
        """remove all bids that are higher than current bid value, which occurs
        when ticker prices come in before depth"""
        while len(self.bids) and self.bids[0].price > bid:
            price = self.bids[0].price
            volume = self.bids[0].volume
            self._update_total_bid(-volume, price)
            self.bids.pop(0)
            self._valid_bid_cache = -1
            # self.debug("### repaired bid")

    def _repair_crossed_asks(self, ask):
        """remove all asks that are lower than official ask value, which occurs
        when ticker prices come in before depth"""
        while len(self.asks) and self.asks[0].price < ask:
            volume = self.asks[0].volume
            self._update_total_ask(-volume)
            self.asks.pop(0)
            self._valid_ask_cache = -1
            # self.debug("### repaired ask")

    def _update_book(self, typ, price, total_vol):
        """update the bids or asks list, insert or remove level and
        also update all other stuff that needs to be tracked such as
        total volumes and invalidate the total volume cache index.
        Return True if book has changed, return False otherwise"""
        (lst, index, level) = self._find_level(typ, price)
        if total_vol == 0:
            if level is None:
                return False
            else:
                voldiff = -level.volume
                lst.pop(index)
        else:
            if level is None:
                voldiff = total_vol
                level = Level(price, total_vol)
                lst.insert(index, level)
            else:
                voldiff = total_vol - level.volume
                if voldiff == 0:
                    return False
                level.volume = total_vol

        # now keep all the other stuff in sync with it
        self.last_change_type = typ
        self.last_change_price = price
        self.last_change_volume = voldiff
        if typ == "ask":
            self._update_total_ask(voldiff)
            if len(self.asks):
                self.ask = self.asks[0].price
            self._valid_ask_cache = min(self._valid_ask_cache, index - 1)
        else:
            self._update_total_bid(voldiff, price)
            if len(self.bids):
                self.bid = self.bids[0].price
            self._valid_bid_cache = min(self._valid_bid_cache, index - 1)

        return True

    def _update_total_ask(self, volume):
        """update total volume of base currency on the ask side"""
        self.total_ask += volume

    def _update_total_bid(self, volume, price):
        """update total volume of quote currency on the bid side"""
        self.total_bid += volume * price

    def _update_level_own_volume(self, typ, price, own_volume):
        """update the own_volume cache in the Level object at price"""

        if price == 0:
            # market orders have price == 0, we don't add them
            # to the orderbook, own_volume is meant for limit orders.
            # Also a price level of 0 makes no sense anyways, this
            # would only insert empty rows at price=0 into the book
            return

        (index, level) = self._find_level_or_insert_new(typ, price)
        if level.volume == 0 and own_volume == 0:
            if typ == "ask":
                self.asks.pop(index)
            else:
                self.bids.pop(index)
        else:
            level.own_volume = own_volume

    def _find_level(self, typ, price):
        """find the level in the orderbook and return a triple
        (list, index, level) where list is a reference to the list,
        index is the index if its an exact match or the index of the next
        element if it was not found (can be used for inserting) and level
        is either a reference to the found level or None if not found."""
        lst = {"ask": self.asks, "bid": self.bids}[typ]
        comp = {"ask": lambda x, y: x < y, "bid": lambda x, y: x > y}[typ]
        low = 0
        high = len(lst)

        # binary search
        while low < high:
            mid = (low + high) // 2
            midval = lst[mid].price
            if comp(midval, price):
                low = mid + 1
            elif comp(price, midval):
                high = mid
            else:
                return (lst, mid, lst[mid])

        # not found, return insertion point (index of next higher level)
        return (lst, high, None)

    def _find_level_or_insert_new(self, typ, price):
        """find the Level() object in bids or asks or insert a new
        Level() at the correct position. Returns tuple (index, level)"""
        (lst, index, level) = self._find_level(typ, price)
        if level:
            return (index, level)

        # no exact match found, create new Level() and insert
        level = Level(price, 0)
        lst.insert(index, level)

        # invalidate the total volume cache at and beyond this level
        if typ == "ask":
            self._valid_ask_cache = min(self._valid_ask_cache, index - 1)
        else:
            self._valid_bid_cache = min(self._valid_bid_cache, index - 1)

        return (index, level)

    def get_own_volume_at(self, price, typ=None):
        """returns the sum of the volume of own orders at a given price. This
        method will not look up the cache in the bids or asks lists, it will
        use the authoritative data from the owns list bacause this method is
        also used to calculate these cached values in the first place."""
        volume = 0
        for order in self.owns:
            if order.price == price and (not typ or typ == order.typ):
                volume += order.volume
        return volume

    def have_own_oid(self, oid):
        """do we have an own order with this oid in our list already?"""
        for order in self.owns:
            if order.oid == oid:
                return True
        return False

    def get_total_up_to(self, price, is_ask):
        """return a tuple of the total volume in coins and in fiat between top
        and this price. This will calculate the total on demand, it has a cache
        to not repeat the same calculations more often than absolutely needed"""
        if is_ask:
            lst = self.asks
            known_level = self._valid_ask_cache
            comp = lambda x, y: x < y
        else:
            lst = self.bids
            known_level = self._valid_bid_cache
            comp = lambda x, y: x > y

        # now first we need the list index of the level we are looking for or
        # if it doesn't match exactly the index of the level right before that
        # price, for this we do a quick binary search for the price
        low = 0
        high = len(lst)
        while low < high:
            mid = (low + high) // 2
            midval = lst[mid].price
            if comp(midval, price):
                low = mid + 1
            elif comp(price, midval):
                high = mid
            else:
                break
        if comp(price, midval):
            needed_level = mid - 1
        else:
            needed_level = mid

        # if the total volume at this level has been calculated
        # already earlier then we don't need to do anything further,
        # we can immediately return the cached value from that level.
        if needed_level <= known_level:
            lvl = lst[needed_level]
            return (lvl._cache_total_vol, lvl._cache_total_vol_quote)

        # we are still here, this means we must calculate and update
        # all totals in all levels between last_known and needed_level
        # after that is done we can return the total at needed_level.
        if known_level == -1:
            total = 0
            total_quote = 0
        else:
            total = lst[known_level]._cache_total_vol
            total_quote = lst[known_level]._cache_total_vol_quote

        # mult_base = self.api.mult_base
        for i in range(known_level, needed_level):
            that = lst[i + 1]
            total += that.volume
            total_quote += that.volume * that.price  # / mult_base
            that._cache_total_vol = total
            that._cache_total_vol_quote = total_quote

        if is_ask:
            self._valid_ask_cache = needed_level
        else:
            self._valid_bid_cache = needed_level

        return (total, total_quote)

    def init_own(self, own_orders):
        """called by api when the initial order list is downloaded,
        this will happen after connect or reconnect"""
        self.owns = []

        # also reset the own volume cache in bids and ask list
        for level in self.bids + self.asks:
            level.own_volume = 0

        if own_orders:
            for order in own_orders:
                if order["currency"] == self.api.curr_quote and order["base"] == self.api.curr_base:
                    self._add_own(Order(
                        order["price"],
                        order["amount"],
                        order["type"],
                        order["oid"],
                        order["status"]
                    ))

        self.orders_updated = time.strftime("%Y-%m-%d %H:%M:%S")
        self.ready_owns = True
        self.signal_changed(self, None)
        self.signal_owns_initialized(self, None)
        self.signal_owns_changed(self, None)

    def add_own(self, order):
        """called by api when a new order has been acked after it has been
        submitted or after a receiving a user_order message for a new order.
        This is a separate method from _add_own because we additionally need
        to fire a bunch of signals when this happens"""
        if not self.have_own_oid(order.oid):
            self.debug("### adding order:", order.typ, order.price, order.volume, order.oid)
            self._add_own(order)
            self.signal_own_added(self, (order))
            self.signal_changed(self, None)
            self.signal_owns_changed(self, None)

    def _add_own(self, order):
        """add order to the list of own orders. This method is used during
        initial download of complete order list."""
        if not self.have_own_oid(order.oid):
            self.owns.append(order)

            # update own volume in that level:
            self._update_level_own_volume(
                order.typ,
                order.price,
                self.get_own_volume_at(order.price, order.typ)
            )
