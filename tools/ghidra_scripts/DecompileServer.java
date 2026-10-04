// Headless decompiler for the workbench. analyzeHeadless starts this after
// analysis and leaves it running. Each connection sends one address and
// receives the length of the pseudo C, then the text.
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.address.AddressSetView;
import ghidra.program.model.listing.Function;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Iterator;

public class DecompileServer extends GhidraScript {
	@Override
	public void run() throws Exception {
		String[] args = getScriptArgs();
		DecompInterface decompiler = new DecompInterface();
		decompiler.openProgram(currentProgram);
		try (ServerSocket server = new ServerSocket(0, 50, InetAddress.getByName("127.0.0.1"))) {
			Files.write(Paths.get(args[0]), Integer.toString(server.getLocalPort()).getBytes(StandardCharsets.UTF_8));
			while (true) {
				Socket socket = server.accept();
				try {
					serve(decompiler, socket);
				} catch (Exception ex) {
					println(ex.toString());
				} finally {
					socket.close();
				}
			}
		} finally {
			decompiler.dispose();
		}
	}

	private void serve(DecompInterface decompiler, Socket socket) throws Exception {
		BufferedReader reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
		String text;
		try {
			text = decompile(decompiler, reader.readLine());
		} catch (Exception ex) {
			text = ex.toString();
		}
		byte[] body = text.getBytes(StandardCharsets.UTF_8);
		OutputStream out = socket.getOutputStream();
		out.write((body.length + "\n").getBytes(StandardCharsets.UTF_8));
		out.write(body);
		out.flush();
	}

	private String decompile(DecompInterface decompiler, String line) {
		if (line == null || line.trim().isEmpty()) {
			return "Missing address.";
		}
		String trimmed = line.trim();
		if (trimmed.regionMatches(true, 0, "SPLIT ", 0, 6)) {
			String[] parts = trimmed.split("\\s+");
			if (parts.length < 3) {
				return "SPLIT needs a start and an end address.";
			}
			String note = splitFunction(parts[1], parts[2]);
			if (!note.isEmpty()) {
				return note;
			}
			decompiler.flushCache();
			trimmed = parts[1];
		}
		String hex = trimmed;
		if (hex.startsWith("0x") || hex.startsWith("0X")) {
			hex = hex.substring(2);
		}
		Address addr;
		try {
			addr = currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(hex);
		} catch (Exception ex) {
			return "Ghidra could not parse " + line.trim() + ".";
		}
		Function func = getFunctionAt(addr);
		if (func == null) {
			func = getFunctionContaining(addr);
		}
		if (func == null) {
			return "Ghidra has no function at " + line.trim() + ".";
		}
		DecompileResults result = decompiler.decompileFunction(func, 60, monitor);
		if (result == null || !result.decompileCompleted() || result.getDecompiledFunction() == null) {
			String err = result == null ? "" : result.getErrorMessage();
			if (err == null || err.isEmpty()) {
				return "Ghidra did not decompile this function.";
			}
			return err;
		}
		return result.getDecompiledFunction().getC();
	}

	private String splitFunction(String startText, String endText) {
		Address start = address(startText);
		Address end = address(endText);
		if (start == null || end == null) {
			return "Ghidra could not parse the split addresses.";
		}
		if (end.compareTo(start) <= 0) {
			return "The split end has to be after the function start.";
		}
		Address last = end.subtract(1);
		int transaction = currentProgram.startTransaction("split");
		boolean ok = false;
		try {
			AddressSet range = new AddressSet(start, last);
			ArrayList entries = new ArrayList();
			Iterator functions = currentProgram.getFunctionManager().getFunctionsOverlapping(range);
			while (functions.hasNext()) {
				Function other = (Function) functions.next();
				if (!other.getEntryPoint().equals(start)) {
					entries.add(other.getEntryPoint());
				}
			}
			for (int i = 0; i < entries.size(); i++) {
				Function other = getFunctionAt((Address) entries.get(i));
				if (other != null) {
					removeFunction(other);
				}
			}
			Function containing = getFunctionContaining(start);
			if (containing != null && containing.getEntryPoint().compareTo(start) < 0
					&& containing.getBody().getMaxAddress().compareTo(start) >= 0) {
				AddressSet kept = new AddressSet(containing.getBody());
				kept.delete(range);
				containing.setBody(kept);
			}
			Function existing = getFunctionAt(start);
			if (existing != null) {
				removeFunction(existing);
			}
			currentProgram.getListing().clearCodeUnits(start, last, false);
			disassemble(start);
			Function function = getFunctionAt(start);
			if (function == null) {
				function = createFunction(start, "_fn_" + start);
			}
			if (function == null) {
				return "Ghidra could not create a function at " + start + ".";
			}
			function.setBody(range);
			AddressSetView body = function.getBody();
			if (body.getMinAddress() == null || !body.getMinAddress().equals(start)
					|| body.getMaxAddress() == null || body.getMaxAddress().compareTo(last) < 0) {
				return "Ghidra kept " + function.getEntryPoint() + " as " + body.getMinAddress() + "-"
						+ body.getMaxAddress() + " instead of " + start + "-" + last + ".";
			}
			ok = true;
			return "";
		} catch (Exception ex) {
			return "Ghidra could not split this function.\n" + ex.getMessage();
		} finally {
			currentProgram.endTransaction(transaction, ok);
		}
	}

	private Address address(String text) {
		String hex = text.trim();
		if (hex.startsWith("0x") || hex.startsWith("0X")) {
			hex = hex.substring(2);
		}
		try {
			return currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(hex);
		} catch (Exception ex) {
			return null;
		}
	}
}
