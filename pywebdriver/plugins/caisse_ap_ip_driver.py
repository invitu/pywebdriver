# Copyright 2026 INVITU (https://www.invitu.com)
# @author: Cyril VINH-TUNG <cyril@invitu.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import json
import socket
from threading import Lock

from flask import jsonify, request

from pywebdriver import app, config, drivers

from .payment_base_driver import PaymentTerminalDriver

CONFIG_SECTION = "caisse_ap_ip_driver"
TERMINAL_ID = "0"
BUFFER_SIZE = 1024

# ISO 4217 numeric code and number of decimals
CURRENCIES = {
    "EUR": ("978", 2),
    "XPF": ("953", 0),
}

FAILURE_LABELS = {
    "00": "Unknown",
    "02": "Phone call required",
    "03": "Forced",
    "04": "Refused",
    "05": "Forbidden",
    "06": "Aborted",
    "07": "Not completed",
    "08": "Input timeout",
    "09": "Message format error",
    "10": "Selection error",
    "11": "Aborted by operator",
    "12": "Unknown action type",
    "13": "Currency not supported",
}


class CaisseAPIPError(Exception):
    pass


def parse_message(data):
    """Return the tags of a complete TLV message, None if more data is needed."""
    tags = {}
    i = 0
    while i < len(data):
        if len(data) < i + 5:
            return None
        size_str = data[i + 2 : i + 5]
        if not size_str.isdigit():
            raise CaisseAPIPError("Invalid answer from the terminal: %s" % data)
        end = i + 5 + int(size_str)
        if end > len(data):
            return None
        tags[data[i : i + 2]] = data[i + 5 : end]
        i = end
    return tags


def build_message(tags):
    # CZ (protocol version) must be the first tag
    items = [("CZ", tags["CZ"])] + [(k, v) for k, v in tags.items() if k != "CZ"]
    return "".join("%s%03d%s" % (tag, len(value), value) for tag, value in items)


class CaisseAPIPDriver(PaymentTerminalDriver):
    def __init__(self):
        super().__init__()
        self.host = config.get(CONFIG_SECTION, "host", fallback=None)
        self.port = config.getint(CONFIG_SECTION, "port", fallback=8888)
        self.timeout = config.getint(CONFIG_SECTION, "timeout", fallback=180)
        self.request_lock = Lock()

    def request_transaction(self, payment_info):
        # A second request while the terminal is busy would be sent to the
        # terminal after the first one and could debit the customer twice.
        with self.request_lock:
            with self.lock:
                transactions = self._get_terminal(TERMINAL_ID)["transactions"]
                busy = any(t["success"] is None for t in transactions.values())
            transaction = self.begin_transaction(TERMINAL_ID)
        if busy:
            self.end_transaction(
                TERMINAL_ID,
                transaction["transaction_id"],
                success=False,
                status="Terminal busy",
            )
        else:
            self.push_task(
                "transaction_start",
                data=dict(
                    payment_info=payment_info,
                    transaction_id=transaction["transaction_id"],
                ),
            )
        return transaction

    def _prepare_request(self, payment_info):
        currency = CURRENCIES.get(payment_info.get("currency_iso"))
        if not currency:
            raise CaisseAPIPError(
                "Currency %s not supported" % payment_info.get("currency_iso")
            )
        code, decimals = currency
        amount = int(round(float(payment_info["amount"]) * 10**decimals))
        if amount <= 0:
            raise CaisseAPIPError("Amount must be positive")
        if len(str(amount)) > 12:
            raise CaisseAPIPError("Amount %s is over the maximum" % amount)
        tags = {
            "CZ": "0300",
            "CJ": "012345678901",
            "CA": "01",
            "CB": str(amount),
            "CD": "0",
            "CE": code,
        }
        if payment_info.get("payment_mode") == "check":
            tags["CC"] = "00C"
        return tags

    def _send(self, message):
        with socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        ) as sock:
            sock.sendall(message.encode("ascii"))
            data = ""
            while True:
                chunk = sock.recv(BUFFER_SIZE)
                if not chunk:
                    break
                data += chunk.decode("ascii")
                answer = parse_message(data)
                if answer and "AE" in answer:
                    return answer
        app.logger.debug("Caisse-AP IP incomplete answer: %s", data)
        raise CaisseAPIPError("Incomplete answer from the terminal")

    def _check_answer(self, request_tags, answer):
        for tag in ("CA", "CD", "CE"):
            if answer.get(tag) != request_tags[tag]:
                raise CaisseAPIPError(
                    "Tag %s is %s in the request and %s in the answer"
                    % (tag, request_tags[tag], answer.get(tag))
                )
        if (answer.get("CB") or "").lstrip("0") != request_tags["CB"]:
            raise CaisseAPIPError(
                "Amount is %s in the request and %s in the answer"
                % (request_tags["CB"], answer.get("CB"))
            )

    def transaction_start(self, data):
        payment_info = data["payment_info"]
        transaction_id = data["transaction_id"]
        result = dict(success=False)
        try:
            if not self.host:
                raise CaisseAPIPError("Terminal host is not configured")
            request_tags = self._prepare_request(payment_info)
            app.logger.info(
                "Caisse-AP IP: sending %s (currency %s) to %s:%s",
                request_tags["CB"],
                request_tags["CE"],
                self.host,
                self.port,
            )
            answer = self._send(build_message(request_tags))
            app.logger.debug("Caisse-AP IP answer: %s", answer)
            if answer["AE"] == "10":
                self._check_answer(request_tags, answer)
                result = dict(
                    success=True,
                    reference="|".join(
                        "{}-{}".format(tag, answer[tag])
                        for tag in ("AA", "AB", "AC", "AI", "CD")
                        if answer.get(tag)
                    ),
                )
            elif answer["AE"] == "01":
                result["status"] = FAILURE_LABELS.get(
                    answer.get("AF"), "Transaction failed"
                )
                result["status_details"] = "AF=%s" % answer.get("AF")
            else:
                raise CaisseAPIPError("Unexpected action status AE=%s" % answer["AE"])
        except socket.timeout:
            result["status"] = "No answer from the terminal"
        except OSError as e:
            result["status"] = "Terminal unreachable"
            result["status_details"] = str(e)
        except Exception as e:
            result["status"] = "Payment terminal error"
            result["status_details"] = str(e)
        if not result["success"]:
            app.logger.warning(
                "Caisse-AP IP transaction %s failed: %s (%s)",
                transaction_id,
                result.get("status"),
                result.get("status_details"),
            )
        self.end_transaction(TERMINAL_ID, transaction_id, **result)


@app.route("/hw_proxy/payment_terminal_transaction_start", methods=["POST"])
def payment_terminal_transaction_start():
    payment_info = json.loads(request.json["params"]["payment_info"])
    transaction = caisse_ap_ip_driver.request_transaction(payment_info)
    return jsonify(jsonrpc="2.0", result=transaction)


caisse_ap_ip_driver = CaisseAPIPDriver()
drivers["caisse_ap_ip_driver"] = caisse_ap_ip_driver
