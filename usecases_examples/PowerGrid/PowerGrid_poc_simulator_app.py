import json
import urllib.parse
from flask import Flask, jsonify, request
from flask import render_template, redirect
from flask import url_for, session, g
from flask import Response, stream_with_context, flash
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix
from app.models.Communicate import Communicate
from app.models.Simulator import Simulator
from app.models.recommendation_store import store as recommendation_store
from app.models.hai_session_state import state as hai_session_state
from config.config import logging, set_pause
from config.env_overrides import describe_active_overrides


app = Flask(__name__, template_folder='app/templates')
app.secret_key = 'votre_clé_secrète_ici'
# x_prefix honours X-Forwarded-Prefix so url_for() generates correct links when
# a reverse proxy serves this app under a path such as /s/<session-id>/gui.
# Without it every generated URL points at the proxy root and the participant's
# first navigation leaves the session (FR-33).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)
socketio = SocketIO(app, cors_allowed_origins='*')

@app.after_request
def add_cors_headers(response):
    """Allow the CAB frontend to POST applied recommendations cross-origin.

    Runs for every response, including the automatic preflight OPTIONS — without
    these headers the browser blocks the apply POST with a CORS error.
    NB: prefer routing the apply through the frontend's nginx same-origin proxy
    (/powergrid-simu/), which avoids CORS entirely; these headers only matter when
    the frontend calls this simulator directly over plain HTTP.
    """
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

com = Communicate()
simu = Simulator(socketio)


@app.route("/")
def index():
    """Display the home page."""
    urls = com.get_cab_server_urls()
    return render_template('index.html', urls=urls)


@app.route('/dashboard')
def dashboard():
    """Display the dashboard."""
    # Get username
    username = session.get('username', 'Invité')
    server = session.get('server', 'Null')
    # Get all messages
    messages = session.pop('message', [])
    return render_template('dashboard.html',
                           username=username,
                           server=server,
                           config=simu.config,
                           com=com,
                           messages=messages)


@app.route('/load_simulation')
def load_simulation():
    """Load the simulation."""
    if 'username' in session:
        simu.load_and_edit_config()
        simu.initialize_simulation(com, session)
        if 'message' not in session or isinstance(session['message'], str):
            session['message'] = []
        session['message'].append("Simulation loaded.")
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/add_server', methods=['POST'])
def add_server():
    """
    Add a new server to the configuration file.
    
    Expects a JSON with 'url' key. Returns JSON with 'success' if successful.
    """
    new_server_url = request.json.get('url')
    if new_server_url:
        # Add the new server to the configuration
        success = com.add_cab_server_url(new_server_url)
        
        if success:
            return jsonify({"success": True})
    
    return jsonify({"success": False})


@app.route('/delete_server', methods=['POST'])
def delete_server():
    """
    Delete a server from the configuration file.
    
    Expects a JSON with 'url' and 'key'. Returns JSON with 'success' status and deleted Url.
    """
    server_url = request.json.get('url')
    if server_url:
        success, deleted_url = com.delete_cab_server_url(server_url)
        return jsonify({"success": success, "deletedUrl": deleted_url})
    return jsonify({"success": False, "deletedUrl": ""})


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handle user login."""
    if request.method == 'POST':
        server = request.form['server_url']
        username = request.form['username']
        password = request.form['password']
        try:
            com.choose_a_cab_application(urllib.parse.unquote(server))
            is_authorized = com.login(username, password)
            if com.cab_url:
                if is_authorized:
                    session['username'] = username
                    session['server'] = server
                    return redirect(url_for('load_simulation'))
                else:
                    flash("Login failed. Please try again.")
            else:
                flash("Server unavailable. Please choose a different server.")
        except ConnectionRefusedError:
            flash("Server refused the connection. Please try again later.")
        except Exception as e:
            flash("An error occurred. Could not establish a connection to the server.")
        
    return redirect(url_for('index'))


@app.route('/edit_config', methods=['POST'])
def edit_config():
    """Modify the simulation configuration."""
    session.pop('message', None)
    new_params = {
        'env_seed': int(request.form['env_seed']),
        'scenario_name': request.form['scenario_name'],
        'assistant_seed': int(request.form['assistant_seed']),
    }
    simu.load_and_edit_config(new_params)
    simu.initialize_simulation(com, session)
    if 'message' not in session or isinstance(session['message'], str):
        session['message'] = []
    session['message'].append("Simulation loaded.")
    return redirect(url_for('dashboard'))


@app.route('/edit_simulation_settings', methods=['POST'])
def edit_simulation_settings():
    """Modify simulation parameters."""
    session.pop('message', None)
    new_params = {
        'refresh_frequency_step': int(request.form['refresh_frequency_step']),
        'time_step_forecast': int(request.form['time_step_forecast']),
        'duration_step_forecast': int(request.form['duration_step_forecast']),
        'step_start_security_analysis': int(request.form['step_start_security_analysis']),
        'stepDuration_s': int(request.form['stepDuration_s']),
        'scenario_first_step': int(request.form['scenario_first_step']),
    }
    new_tempo = int(request.form['tempo'])
    simu.load_and_edit_config(new_params)
    com.edit_parameters('Outputs.Context.tempo', new_tempo)
    simu.initialize_simulation(com, session)
    if 'message' not in session or isinstance(session['message'], str):
        session['message'] = []
    session['message'].append("Simulation loaded.")
    return redirect(url_for('dashboard'))


socketio.start_background_task(simu.run_simulator, com)


def _tracked_simulation_stream(simulation_stream):
    """
    Wrap the simulation stream so session state follows the participant.

    ``run_simulator`` is a generator: its body does not execute until something
    iterates it, which happens when this stream is consumed by the browser.
    Wrapping it is therefore the only accurate place to observe that a session
    has actually started, and the ``finally`` clause is the only place that
    catches every way it can end — episode complete, exception, or the
    participant closing the tab (FR-34).

    Args:
        simulation_stream: The generator returned by ``simu.run_simulator``.

    Yields:
        Each chunk of the original stream, unmodified.
    """
    hai_session_state.mark_running()
    try:
        for simulation_chunk in simulation_stream:
            yield simulation_chunk
    finally:
        hai_session_state.mark_finished()


@app.route('/start_simulation', methods=['GET'])
def start_simulation():
    """Start the simulation."""
    if 'username' in session:
        if not hasattr(g, 'thread_started') or not g.thread_started:
            response = Response(
                stream_with_context(
                    _tracked_simulation_stream(simu.run_simulator(com))),
                mimetype='text/event-stream')
            response.headers['Cache-Control'] = 'no-cache'
            response.headers['Connection'] = 'keep-alive'
            response.headers['X-Accel-Buffering'] = 'no'
            return response
    return redirect(url_for('dashboard'))


@app.route('/pause_simulation', methods=['POST'])
def pause_simulation():
    """Pause the simulation."""
    set_pause(True)
    return "Paused", 200


@app.route('/continue_simulation', methods=['POST'])
def continue_simulation():
    """Resume the simulation after a pause."""
    set_pause(False)
    return "Continued", 200


@app.route('/logout')
def logout():
    """Log out the user."""
    # Clear session data
    session.pop('username', None)
    session.pop('config', None)
    session.pop('act', None)
    session.pop('message', None)
    # Redirect to login page
    return redirect(url_for('index'))


@app.route('/reset_simulation', methods=['POST'])
def reset_simulation():
    """Reset the simulation."""
    simu.initialize_simulation(com, session)
    return jsonify({"message": "Simulation reset successfully"})


@app.route('/get-last-payloads')
def get_last_payloads():
    """Retrieve the latest payloads."""
    return jsonify(com.list_of_issues)


@app.route('/api/v1/recommendations', methods=['POST'])
def receive_act():
    """Receive a recommendation from InteractiveAI and store it in memory.

    The simulation loop reads it back directly from the shared store, so this
    endpoint is the single entry point for recommendations.
    """
    data = request.get_json()
    recommendation_store.set(data)
    logging.info("Recommendation received via POST from InteractiveAI: %s",
                 json.dumps(data))
    return jsonify({
        "message": "OK"
    })


@app.route('/api/v1/recommendations', methods=['GET'])
def send_act():
    """Return and clear the stored recommendation.

    Kept for external callers; the simulation loop no longer relies on this
    endpoint and reads the shared store directly instead.
    """
    act_dict = recommendation_store.pop()
    logging.info("Recommendation served via GET: %s", json.dumps(act_dict))
    return jsonify(act_dict)


# ---------------------------------------------------------------------------
# WP3 Human-AI session control API (FR-34)
#
# These three endpoints let an external control plane bind this simulator to a
# session, release it again, and poll what it is doing — without restarting the
# container. They exist so a fixed pool of simulator containers can be
# reassigned between participants, which removes the need for anything to
# create containers at runtime and therefore removes the Docker socket from the
# deployment entirely.
#
# They are deliberately thin: configuration and initialisation go through the
# same simu.load_and_edit_config() and simu.initialize_simulation() calls that
# the existing /edit_config form route has always used. Nothing here changes
# behaviour for an operator driving the app through its own UI.
# ---------------------------------------------------------------------------

# Fields a caller may set on POST /hai/session, mapped to CONFIG.toml keys.
# Restricting the accepted set keeps an external caller from writing arbitrary
# keys into the simulator's configuration dictionary.
HAI_SESSION_CONFIG_FIELDS = {
    "scenario_name": str,
    "env_name": str,
    "env_seed": int,
    "assistant_path": str,
    "assistant_seed": int,
    "scenario_first_step": int,
    "step_start_security_analysis": int,
    "refresh_frequency_step": int,
    "time_step_forecast": int,
    "duration_step_forecast": int,
    "stepDuration_s": float,
}


def _parse_hai_session_config(request_body):
    """
    Extract and type-convert the configuration fields of a session request.

    Args:
        request_body: Parsed JSON body of POST /hai/session.

    Returns:
        Tuple of (config_parameters, error_message). ``error_message`` is None
        when parsing succeeded; when it is set, ``config_parameters`` is empty
        and the caller should reject the request.
    """
    config_parameters = {}

    for field_name, field_type in HAI_SESSION_CONFIG_FIELDS.items():
        if field_name not in request_body:
            continue

        raw_value = request_body[field_name]
        try:
            config_parameters[field_name] = field_type(raw_value)
        except (TypeError, ValueError):
            return {}, "Field '{}' must be of type {}, got {!r}".format(
                field_name, field_type.__name__, raw_value)

    return config_parameters, None


@app.route('/hai/session', methods=['POST'])
def hai_configure_session():
    """Bind this simulator to a WP3 session and load its scenario.

    Configures the simulation from the request body and initialises the
    grid2op environment, so the participant who opens the GUI afterwards lands
    on a scenario prepared for them. Replaces what previously required starting
    a container with per-session environment variables.

    Returns 409 when a session is already bound, so a control plane can never
    silently overwrite a running participant's session — the slot must be reset
    first.
    """
    request_body = request.get_json(silent=True)
    if not isinstance(request_body, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    session_id = request_body.get("session_id")
    if not session_id:
        return jsonify({"error": "Field 'session_id' is required"}), 400

    if hai_session_state.is_occupied():
        current_state = hai_session_state.snapshot()
        return jsonify({
            "error": "This simulator is already bound to a session",
            "current": current_state,
        }), 409

    config_parameters, parse_error = _parse_hai_session_config(request_body)
    if parse_error is not None:
        return jsonify({"error": parse_error}), 400

    cab_url = request_body.get("cab_url")
    if cab_url:
        com.cab_url = cab_url

    try:
        simu.load_and_edit_config(config_parameters or None)
        simu.initialize_simulation(com, session)
    except Exception as initialisation_error:  # noqa: BLE001
        # Leave the simulator idle rather than half-configured, so the control
        # plane can retry on this same container or pick another one.
        logging.exception("Failed to initialise session %s", session_id)
        simu.release_environment()
        hai_session_state.reset()
        return jsonify({
            "error": "Failed to initialise the simulation",
            "detail": str(initialisation_error),
        }), 500

    loaded_scenario_name = None
    if simu.env is not None:
        loaded_scenario_name = simu.env.chronics_handler.get_name()

    hai_session_state.mark_configured(session_id, loaded_scenario_name)
    logging.info(
        "Session %s configured: scenario=%s", session_id, loaded_scenario_name)

    return jsonify(hai_session_state.snapshot()), 200


@app.route('/hai/reset', methods=['POST'])
def hai_reset_session():
    """Release the bound session and return this simulator to the free pool.

    Closes the grid2op environment so the LightSim backend it holds is freed
    rather than leaked, then clears the session binding. Idempotent: resetting
    an already-idle simulator succeeds and reports the idle state.
    """
    previous_state = hai_session_state.snapshot()

    simu.release_environment()
    hai_session_state.reset()

    logging.info(
        "Session %s released; simulator returned to the pool",
        previous_state.get("session_id"))

    return jsonify({
        "released": previous_state,
        "current": hai_session_state.snapshot(),
    }), 200


@app.route('/hai/state', methods=['GET'])
def hai_read_state():
    """Report which session this simulator is serving and how far it has got.

    Polled by the WP3 control plane to detect a finished episode and to verify
    that a slot really is free before assigning it.
    """
    return jsonify(hai_session_state.snapshot()), 200


if __name__ == '__main__':
    logging.info(describe_active_overrides())
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True, host='0.0.0.0', port=5000)