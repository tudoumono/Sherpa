<%@ page import="java.util.List" %>
<%@ include file="common/header.jspf" %>
<%@ taglib tagdir="/WEB-INF/tags" %>
<html>
<head>
<script src="../../static/app.js"></script>
<link href="../../static/style.css">
</head>
<body>
<%-- login form (Struts) --%>
<s:form action="login">
  <input type="text" name="user">
</s:form>
<a href="/orders/list">Orders</a>
<jsp:useBean id="loginBean" class="com.acme.LoginAction" />
<%
  int x = 1;
  out.print(x);
%>
</body>
</html>
