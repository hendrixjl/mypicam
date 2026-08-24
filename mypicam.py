from http.server import HTTPServer, BaseHTTPRequestHandler

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    # Handle incoming GET requests
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        
        # The HTML content sent back to the browser
        html = "<html><body><h1>Hello from Python!</h1></body></html>"
        self.wfile.write(bytes(html, "utf8"))

# Configure the server address and port
server_address = ("localhost", 8000)
httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)

print("Server running on http://localhost:8000...")
try:
    httpd.serve_forever()
except KeyboardInterrupt:
    print("\nServer stopped.")
    httpd.server_close()
