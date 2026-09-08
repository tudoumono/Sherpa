import { formatDate } from "./utils.js";

function loadOrders() {
  return fetch("/api/orders").then(function (r) {
    return r.json();
  });
}

function removeOrder(id) {
  return fetch("/api/" + id, { method: "DELETE" });
}

function loadOrderList() {
  return fetch("/orders/list");
}
